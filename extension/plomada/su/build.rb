# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative 'builders'
require_relative 'storeys'
require_relative 'library'
require_relative 'site'
require_relative 'railings'
require_relative 'planters'
require_relative 'columns'
require_relative 'pools'
require_relative 'ramps'
require_relative '../plan'
require_relative '../jobs'
require_relative '../geometry'

module Plomada
  module SU
    # The flagship: a plan (walls, openings, rooms) becomes a house in one
    # job. All geometry is solved in pure Ruby when the job is created, so an
    # invalid plan is refused with -32004 before anything touches the model;
    # then each step does one small unit (one wall, one opening, one label).
    module Build
      ROOFS = %w[flat gable hip none].freeze
      KINDS_REPLACED = %w[walls opening slab slab_band roof room stair furniture site
                          column curtain_wall railing planter pool ramp].freeze
      FURNITURE_MODES = %w[library massing].freeze
      BLONDEL_MM = (600.0..650.0).freeze # 2R + G, comfortable stairs

      module_function

      # --- options --------------------------------------------------------------------

      def options(raw, config = CONFIG)
        raw ||= {}
        raise InvalidParams, "options must be an object, got #{Plan.type_name(raw)}" unless raw.is_a?(Hash)

        {
          'storey_height' => opt_num(raw, 'storey_height', nil),
          'slab' => opt_bool(raw, 'slab', true),
          'roof' => Plan.choice(raw.fetch('roof', 'flat') || 'flat', 'options.roof', ROOFS),
          'overhang' => opt_num(raw, 'overhang', config[:roof_overhang_mm], zero: true),
          'replace' => opt_bool(raw, 'replace', true),
          'slab_thickness' => opt_num(raw, 'slab_thickness', config[:slab_thickness_mm]),
          'roof_thickness' => opt_num(raw, 'roof_thickness', config[:roof_thickness_mm]),
          'pitch' => opt_num(raw, 'pitch', config[:gable_pitch_deg]),
          'fit_view' => opt_bool(raw, 'fit_view', true),
          'storey' => raw['storey'].nil? ? nil : storey_name(raw['storey']),
          'elevation' => raw['elevation'].nil? ? nil : Plan.elevation(raw['elevation'], 'options.elevation', config),
          'ground_level_mm' => raw['ground_level_mm'].nil? ? nil : Plan.elevation(raw['ground_level_mm'], 'options.ground_level_mm', config),
          'library' => raw['library'].nil? ? nil : Plan.text(raw['library'], 'options.library'),
          'furniture' => Plan.choice(raw.fetch('furniture', 'library') || 'library', 'options.furniture', FURNITURE_MODES)
        }.tap do |o|
          raise InvalidParams, "options.pitch must be less than 75, got #{Plan.fmt(o['pitch'])}" if o['pitch'] >= 75
        end
      end

      def storey_name(value, path = 'options.storey')
        name = Plan.text(value, path)
        return name if name.match?(/\A[A-Za-z0-9_-]{1,16}\z/)

        raise InvalidParams, "#{path} must be 1 to 16 letters, digits, - or _ (like N01), got #{value.inspect}"
      end

      def opt_num(raw, key, default, zero: false)
        return default if raw[key].nil?

        zero ? Plan.non_negative(raw[key], "options.#{key}") : Plan.positive(raw[key], "options.#{key}")
      end

      def opt_bool(raw, key, default)
        return default unless raw.key?(key) && !raw[key].nil?
        return raw[key] if [true, false].include?(raw[key])

        raise InvalidParams, "options.#{key} must be true or false, got #{raw[key].inspect}"
      end

      # --- preparation (pure, before any model change) ------------------------------------

      # Normalizes the plan, solves the walls and the roof. Raises InvalidParams.
      # A plan whose storey has elevation_mode explicit carries its own
      # elevation and a floor-to-floor height (its walls stop under the next
      # slab); options.elevation and options.storey_height still win when given
      # for a plan without one.
      def prepare(raw_plan, opts)
        raw = raw_plan.is_a?(Hash) ? raw_plan.dup : raw_plan
        own = raw.is_a?(Hash) && raw['storey'].is_a?(Hash) && raw['storey']['elevation_mode'] == 'explicit' &&
              !raw['storey']['height'].nil?
        if raw.is_a?(Hash) && opts['storey_height'] && !own
          raw['storey'] = (raw['storey'] || {}).merge('height' => opts['storey_height'])
        end
        raw['storey'] = (raw['storey'] || {}).merge('name' => opts['storey']) if raw.is_a?(Hash) && opts['storey']
        plan = Plan.normalize(raw)
        height = plan['storey']['height']
        if plan['storey']['elevation_mode'] == 'explicit'
          opts['elevation'] = plan['storey']['elevation'] if opts['elevation'].nil?
          opts['elevation_mode'] = 'explicit'
          opts['floor_to_floor'] = height
          height = Geometry::Levels.wall_height(height, opts['slab_thickness'])
        end
        warnings = []
        library = opts['furniture'] == 'library' && Library.map(opts['library']) ? opts['library'] : nil
        if opts['furniture'] == 'library' && library.nil? && (plan['furniture'].any? || plan['site']['objects'].any?)
          warnings << "no component library at #{opts['library'].inspect}: furniture stays massing and site objects are skipped"
        end
        known, unknown = plan['furniture'].partition { |f| Geometry.furniture_item?(f['item']) }
        unless unknown.empty?
          warnings << "#{unknown.size} furniture block(s) skipped, no massing for: #{unknown.map { |f| f['item'] }.uniq.join(', ')}"
        end
        stairs = plan['stairs'].map do |st|
          lay = Geometry.stair_layout(st)
          warnings.concat(stair_warnings(st, lay, height + opts['slab_thickness']))
          [st, lay]
        end
        { plan: plan, buildings: Geometry.building_plans(plan['walls'], plan['openings']), stairs: stairs,
          furniture: known, library: library, warnings: warnings, height: height,
          library_dir: opts['library'] && Library.map(opts['library']) ? opts['library'] : nil }
      end

      # One job step per building: the wall solve is the costly part, and a
      # plan of several buildings would otherwise hold the UI in one tick.
      # These steps change nothing, so a plan the solver refuses leaves the
      # model as it was.
      def solve_units(buildings, height, state)
        state[:layouts] = []
        buildings.each_with_index.map do |(ws, os), i|
          ["solve building #{i + 1} of #{buildings.size}",
           -> { state[:layouts] << Geometry::WallSolver.new(ws, os, storey_height: height).solve }]
        end
      end

      # Merges the buildings' layouts into +prep+ and solves the roofs: one
      # slab and one roof per building. A gable needs every footprint
      # rectangular; a hip takes any simple outline.
      def solved!(prep, layouts, opts)
        layout = Geometry.merge_layouts(layouts)
        prep[:warnings].concat(layout[:warnings])
        outlines = layout[:exteriors] || [layout[:exterior]].compact
        if outlines.empty? && (opts['slab'] || opts['roof'] != 'none')
          prep[:warnings] << 'no closed exterior wall: slab and roof skipped'
        end
        pitched = outlines.map { |outline| pitched_roof(opts['roof'], outline, prep[:height], opts) }
        prep.merge!(layout: layout, outlines: outlines, outline: outlines.first, pitched: pitched)
      end

      # The solved gable or hip roof of one building, nil for flat or none.
      def pitched_roof(kind, outline, height, opts)
        case kind
        when 'gable'
          Geometry.gable_roof(outline[:points], height, opts['overhang'], opts['pitch'], opts['roof_thickness'],
                              outline[:thickness])
        when 'hip'
          Geometry.hip_roof(outline[:points], height, opts['overhang'], opts['pitch'], opts['roof_thickness'])
        end
      end

      # A stair should climb exactly one floor to floor (storey height plus the
      # next slab) with comfortable steps; neither is a reason to refuse it.
      def stair_warnings(stair, lay, floor_to_floor)
        out = []
        if (lay[:top] - floor_to_floor).abs > 5.0
          out << "stair #{stair['id']} climbs #{Plan.fmt(lay[:top])} mm (#{stair['risers']} x #{Plan.fmt(stair['riser_height'])}) "                  "but floor to floor is #{Plan.fmt(floor_to_floor)} mm (storey height plus slab)"
        end
        unless BLONDEL_MM.cover?(lay[:blondel])
          out << "stair #{stair['id']}: 2R + G = #{Plan.fmt(lay[:blondel])} mm, outside the comfortable 600 to 650"
        end
        out
      end

      # N00_losa for the first building, N00_losa_2, N00_losa_3 ... for the rest.
      def building_group(storey, part, index) = SU.group_name(storey, part) + (index.zero? ? '' : "_#{index + 1}")

      def wall_record(w, height)
        { 'v' => 1, 'kind' => 'wall' }.merge(w).merge('height' => w['height'] || height)
      end

      def opening_record(o) = { 'v' => 1, 'kind' => 'opening' }.merge(o)
      def room_record(r) = { 'v' => 1, 'kind' => 'room' }.merge(r)

      # --- the build_plan job ---------------------------------------------------------

      def job(params, ctx, label: 'Plomada: build plan')
        opts = options(params['options'])
        prep = prepare(params['plan'], opts)
        model = ctx.model
        plan = prep[:plan]
        storey = plan['storey']['name']
        state = { group: nil, faces: 0, removed: 0, manifold: nil, groups: [], elevation: 0.0, wells: [],
                  roof: opts['roof'] }
        units = solve_units(prep[:buildings], prep[:height], state)
        at = units.size
        units << ['plan solved', lambda {
          solved!(prep, state[:layouts], opts)
          units.insert(at + 1, *model_units(model, prep, opts, state, storey))
        }]
        UnitJob.new(label, units) do
          ops = plan['openings']
          {
            'walls' => plan['walls'].size, 'openings' => ops.size,
            'doors' => ops.count { |o| o['opening_kind'] == 'door' },
            'windows' => ops.count { |o| o['opening_kind'] == 'window' },
            'rooms' => plan['rooms'].size, 'stairs' => plan['stairs'].size, 'furniture' => prep[:furniture].size,
            'furniture_source' => prep[:library] ? 'library' : 'massing',
            'site' => plan['site'].transform_values(&:size).merge('objects_placed' => state[:site] || 0),
            'wall_faces' => state[:faces],
            'internal_faces_removed' => state[:removed], 'manifold' => state[:manifold],
            'groups' => state[:groups], 'storey' => storey, 'elevation' => state[:elevation],
            'storey_height' => prep[:height], 'roof' => state[:roof], 'stair_wells' => state[:wells].size,
            'warnings' => prep[:warnings], 'undo' => "one step: #{label}"
          }
        end
      end

      # The steps that build the storey once its plan is solved.
      def model_units(model, prep, opts, state, storey)
        plan = prep[:plan]
        layout = prep[:layout]
        units = []
        units << ['materials and tags', lambda {
          SU.ensure_tags(model)
          SU.ensure_materials(model)
          place_storey(model, storey, opts, prep, state)
          erase_previous(model, storey) if opts['replace']
          state[:before] = Storeys.snapshot(model)
        }]
        units.concat(wall_units(model, plan, layout, storey, prep[:height], state))
        units << ['strip internal faces', lambda {
          finish_walls(state, layout, plan, opts.merge('elevation' => state[:elevation]), prep[:height])
        }]
        layout[:openings].each do |frame|
          units << ["opening #{frame[:id]}", lambda {
            Openings.place(model, frame.merge(record: opening_record(frame[:record])))
          }]
        end
        if plan['slabs'].any?
          # Slab records replace the automatic slab under the exterior wall.
          units << ["floor slabs #{plan['slabs'].map { |r| r['id'] }.join(', ')}", lambda {
            lay = Geometry.slab_layout(plan['slabs'], state[:wells])
            prep[:warnings].concat(lay[:warnings])
            state[:groups].concat(Slabs.records(model, storey, lay, plan['slabs'], prep[:warnings]))
          }]
        else
          prep[:outlines].each_with_index do |outline, i|
            next unless opts['slab']

            units << ["floor slab #{outline[:wall]}", lambda {
              name = building_group(storey, 'losa', i)
              Slabs.slab(model, storey, outline[:points], opts['slab_thickness'], name: name, holes: state[:wells])
              state[:groups] << name
            }]
          end
        end
        prep[:outlines].each_with_index do |outline, i|
          next if opts['roof'] == 'none'

          units << ["#{opts['roof']} roof #{outline[:wall]}", lambda {
            next if state[:roof] == 'none' # a storey already sits on top of this one

            name = building_group(storey, 'techo', i)
            if prep[:pitched][i]
              Slabs.pitched_roof(model, storey, prep[:pitched][i], opts['roof'], opts['roof_thickness'], opts['overhang'],
                                 opts['pitch'], name: name)
            else
              Slabs.flat_roof(model, storey, outline[:points], prep[:height], opts['roof_thickness'], opts['overhang'],
                              name: name)
            end
            state[:groups] << name
          }]
        end
        prep[:stairs].each do |st, lay|
          units << ["stair #{st['id']}", lambda {
            g = Stairs.build(model, storey, st, lay)
            state[:groups] << g.name
          }]
        end
        plan['rooms'].each do |room|
          units << ["room #{room['id']}", -> { Rooms.label(model, room_record(room)) }]
        end
        # A library piece loads a .skp (one native call): one per step. Massing goes eight at a time.
        prep[:furniture].each_slice(prep[:library] ? 1 : 8) do |batch|
          units << ["furniture #{batch.map { |f| f['id'] }.join(', ')}", lambda {
            batch.each { |f| Furniture.place(model, f, library: prep[:library]) }
          }]
        end
        units.concat(part_units(model, plan, prep, state, storey))
        units.concat(site_units(model, plan['site'], prep, opts, state))
        units << ['raise to storey', -> { Storeys.stamp_and_lift(model, state[:before], storey, state[:elevation]) }]
        units << ['view', -> { View.fit(model) if opts['fit_view'] }]
        units
      end

      # The 0.5 parts, one step each (columns eight at a time): railings,
      # planters and pools stand on the slab they name (or the storey floor).
      def part_units(model, plan, prep, state, storey)
        footing = Footing.of(plan)
        add = ->(g) { state[:groups] << g.name }
        units = plan['columns'].each_slice(8).map do |batch|
          ["columns #{batch.map { |c| c['id'] }.join(', ')}", -> { batch.each { |c| add.call(Columns.build(model, storey, c, prep[:height])) } }]
        end
        units += plan['ramps'].map { |r| ["ramp #{r['id']}", -> { add.call(Ramps.build(model, storey, r)) }] }
        units += plan['pools'].map do |r|
          ["pool #{r['id']}", -> { add.call(Pools.build(model, storey, r, footing, state[:site_ok])) }]
        end
        units += plan['railings'].map { |r| ["railing #{r['id']}", -> { add.call(Railings.build(model, storey, r, footing)) }] }
        units + plan['planters'].map do |r|
          ["planter #{r['id']}", -> { add.call(Planters.build(model, storey, r, footing, prep[:library_dir], prep[:warnings])) }]
        end
      end

      # The site stands on the terrain, so only the storey closest to the
      # ground level builds it (place_storey decides), or any storey whose plan
      # says site_on_ground, which puts it at the ground level itself.
      def site_units(model, site, prep, _opts, state)
        units = []
        guard = lambda do |label, &work|
          units << [label, lambda {
            if state[:site_ok]
              work.call
            else
              prep[:warnings] << "#{label} skipped: the site belongs on the ground storey's DXF" unless state[:site_warned]
              state[:site_warned] = true
            end
          }]
        end
        hard = (site['paving'] + site['decks']).map { |r| Geometry::Arc.polyline(r['points'], closed: true) }
        site['objects'].each do |o|
          guard.call("site #{o['item']} #{o['id']}") do
            if Site.object(model, o, prep[:library], Geometry.site_base(o['at'], hard, state[:site_ground]))
              state[:site] = (state[:site] || 0) + 1
            else
              prep[:warnings] << "site object #{o['item']} (#{o['id']}): no component in the library"
            end
          end
        end
        site['paving'].each { |r| guard.call("paving #{r['id']}") { Site.paving(model, r, state[:site_ground]) } }
        site['decks'].each { |r| guard.call("deck #{r['id']}") { Site.paving(model, r, state[:site_ground], deck: true) } }
        site['fences'].each { |r| guard.call("fence #{r['id']}") { Site.fence(model, r, state[:site_ground]) } }
        site['pools'].each { |r| guard.call("pool #{r['id']}") { Site.pool(model, r, state[:site_ground]) } }
        units
      end

      # Where the storey goes, decided when the job starts (before replace
      # erases it): its elevation, the stair wells its slab needs, the roof of
      # the storey it lands on (erased: the new slab takes its place), and no
      # roof of its own when another storey already sits on top.
      def place_storey(model, storey, opts, prep, state)
        elevation = Storeys.elevation_for(model, storey, opts['elevation'], opts['slab_thickness'])
        state[:elevation] = elevation
        state[:wells] = Storeys.wells(model, elevation, storey)
        place_site(model, storey, elevation, opts, prep, state)
        under = Storeys.below(model, elevation, storey)
        if under && (under['elevation'] + under['height']) >= elevation - opts['slab_thickness'] - 1.0
          roofs = Storeys.entities(model, under['name']).select { |e| SU.kind(e) == 'roof' }
          unless roofs.empty?
            model.entities.erase_entities(roofs)
            prep[:warnings] << "roof of #{under['name']} erased: #{storey} now sits on it"
          end
        end
        above = Storeys.list(model).find { |s| s['name'] != storey && s['elevation'] > elevation + 1.0 }
        return unless above

        if state[:roof] != 'none'
          state[:roof] = 'none'
          prep[:warnings] << "no roof on #{storey}: #{above['name']} sits on top of it"
        end
        return if prep[:stairs].empty?

        prep[:warnings] << "build #{above['name']} again to cut the stair wells of #{storey} into its slab"
      end

      # The ground level (an option stores it in the model for later builds and
      # add_terrain), whether this storey builds the site and at what local z.
      def place_site(model, storey, elevation, opts, prep, state)
        Storeys.store_ground_level(model, opts['ground_level_mm']) unless opts['ground_level_mm'].nil?
        ground_level = Storeys.ground_level(model)
        on_ground = prep[:plan]['site_on_ground']
        storeys = Storeys.list(model).reject { |s| s['name'] == storey } +
                  [{ 'name' => storey, 'elevation' => elevation, 'height' => prep[:height] }]
        state[:ground_level] = ground_level
        state[:site_ok] = on_ground || Geometry::Levels.site_storey(storeys, ground_level) == storey
        state[:site_ground] = Geometry::Levels.site_ground(elevation, opts['slab_thickness'], ground_level, on_ground)
      end

      def wall_units(model, plan, layout, storey, height, state)
        plan['walls'].map do |w|
          faces = layout[:faces].select { |f| f[:wall] == w['id'] }
          ["wall #{w['id']}", lambda {
            unless state[:group]
              state[:group] = Walls.create_group(model, storey)
              state[:groups] << state[:group].name
            end
            state[:faces] += Walls.build_wall(state[:group], faces, wall_record(w, height))
          }]
        end
      end

      def finish_walls(state, layout, plan, opts, height)
        group = state[:group]
        return unless group

        state[:removed] = Walls.strip_internal_faces(group, layout[:solids])
        state[:softened] = Walls.soften_curves(group)
        state[:manifold] = group.manifold?
        settings = { 'name' => plan['storey']['name'], 'height' => height, 'roof' => opts['roof'],
                     'elevation' => opts['elevation'].to_f,
                     'overhang' => opts['overhang'], 'slab' => opts['slab'],
                     'slab_thickness' => opts['slab_thickness'], 'roof_thickness' => opts['roof_thickness'],
                     'pitch' => opts['pitch'] }
        settings['elevation_mode'] = 'explicit' if opts['elevation_mode'] == 'explicit'
        settings['floor_to_floor'] = opts['floor_to_floor'] if opts['floor_to_floor']
        SU.set_attrs(group, 'storey_settings' => JSON.generate(settings))
        finishes = state[:finishes] || {}
        Edit.apply_finishes(group, finishes) unless finishes.empty?
      end

      # replace: erases only this storey's top-level groups and components that
      # carry a plomada attribute; other storeys stay.
      def erase_previous(model, storey = Storeys.default_name)
        doomed = Storeys.entities(model, storey).select { |e| KINDS_REPLACED.include?(SU.kind(e)) }
        model.entities.erase_entities(doomed) unless doomed.empty?
        doomed.size
      end
    end

    # Camera helpers shared by the build, capture and scene tools.
    module View
      module_function

      FIT_DIR = [-0.62, -0.95, 0.55].freeze # from the target toward the eye: south-west, above
      FIT_ASPECT = 1.5                      # width / height the framing must fit (exports are 3:2)
      FIT_MARGIN = 1.06                     # breathing room around the bounding box

      # A south-west three-quarter view framing everything Plomada built: the
      # eye backs off along FIT_DIR until all eight bounding-box corners fall
      # inside the vertical field of view and the 3:2 horizontal one.
      def fit(model, fov = CONFIG[:scene_fov_deg])
        bb = Geom::BoundingBox.new
        SU.plomada_entities(model).each { |e| bb.add(e.bounds) unless SU.kind(e) == 'terrain' }
        bb = model.bounds if bb.empty?
        return if bb.empty?

        center = bb.center
        dir = Geom::Vector3d.new(*FIT_DIR).normalize
        forward = dir.reverse
        right = forward.cross(Z_AXIS).normalize
        up = right.cross(forward).normalize
        tan_v = Math.tan(fov * Math::PI / 360.0)
        tan_h = tan_v * FIT_ASPECT
        dist = (0..7).map do |i|
          o = bb.corner(i) - center
          depth_off = o.dot(forward)
          [o.dot(right).abs / tan_h, o.dot(up).abs / tan_v].max - depth_off
        end.max * FIT_MARGIN
        eye = center.offset(dir, dist)
        model.active_view.camera = Sketchup::Camera.new(eye, center, Z_AXIS, true, fov)
      end
    end
  end
end
