# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative 'builders'
require_relative 'build'
require_relative 'storeys'
require_relative 'site'
require_relative 'ramps'

module Plomada
  module SU
    # Tools that change a house already in the model. Each reads the plan of
    # one storey back from the plomada attributes (no DXF), changes one record,
    # re-solves the whole storey in pure Ruby (so a bad edit is refused before
    # any change) and rebuilds only what the edit touches, as one undo step.
    # New geometry is built at z 0 and raised to the storey's elevation last.
    module Edit
      module_function

      PARAMS_NOT_RECORD = %w[deadline_ms storey].freeze

      def current_plan(model, storey, require_walls: true)
        plan = PlanReader.read(model, storey)
        if require_walls && plan['walls'].empty?
          raise InvalidParams, "no Plomada walls on storey #{storey}; build one with build_from_autocad or build_plan first"
        end

        plan
      end

      def walls_group(model, storey)
        walls_groups(model).find { |g| Storeys.of(g) == storey }
      end

      def walls_groups(model)
        model.entities.grep(Sketchup::Group).select { |g| g.valid? && SU.kind(g) == 'walls' }
      end

      def finishes(group)
        raw = group&.get_attribute(DICT, 'finishes')
        raw ? JSON.parse(raw) : {}
      end

      def apply_finishes(group, finishes)
        finishes.each do |wall_id, mat_name|
          mat = SU.material(group.model, mat_name)
          group.entities.grep(Sketchup::Face).each do |f|
            f.material = mat if f.get_attribute(DICT, 'wall') == wall_id
          end
        end
        SU.set_attrs(group, 'finishes' => JSON.generate(finishes))
      end

      # Returns the units that solve the edited plan (one building per step,
      # changing nothing, so a refused edit leaves the model as it was) and
      # then replace the storey's walls group; state[:layout] holds the solved
      # layout from the 'plan solved' step on. The caller appends its own
      # units, then raise_unit.
      def rebuild(model, raw_plan, storey)
        settings = PlanReader.storey_settings(model, storey)
        raw = raw_plan.merge('storey' => { 'name' => storey, 'height' => settings['height'] })
        plan = Plan.normalize(raw)
        height = plan['storey']['height']
        old = walls_group(model, storey)
        state = { group: nil, faces: 0, removed: 0, manifold: nil, groups: [], finishes: finishes(old),
                  elevation: settings.fetch('elevation', 0.0).to_f }
        units = Build.solve_units(Geometry.building_plans(plan['walls'], Geometry.wall_openings(plan, height)), height, state)
        at = units.size
        units << ['plan solved', lambda {
          layout = state[:layout] = Geometry.merge_layouts(state[:layouts])
          walls = [['remove old walls', lambda {
            model.entities.erase_entities(old) if old&.valid?
            state[:before] = Storeys.snapshot(model)
          }]]
          walls.concat(Build.wall_units(model, plan, layout, storey, height, state))
          walls << ['strip internal faces', -> { Build.finish_walls(state, layout, plan, settings, height) }]
          units.insert(at + 1, *walls)
        }]
        [plan, units, state]
      end

      def frame_of(state, id) = frames_of(state, id).first

      # Every frame of opening +id+: one, or one per panel on a curved wall.
      def frames_of(state, id) = state[:layout][:openings].select { |f| f[:id] == id }

      def raise_unit(model, state, storey)
        ['raise to storey', -> { Storeys.stamp_and_lift(model, state[:before], storey, state[:elevation]) }]
      end

      def summary(state, storey)
        { 'storey' => storey, 'wall_faces' => state[:faces], 'internal_faces_removed' => state[:removed],
          'manifold' => state[:manifold] }
      end

      def add_wall(params, ctx)
        model = ctx.model
        storey = Storeys.resolve(model, params['storey'])
        plan = current_plan(model, storey, require_walls: false)
        wall = params.reject { |k, _| PARAMS_NOT_RECORD.include?(k) }
        if plan['walls'].any? { |w| w['id'] == wall['id'] }
          raise InvalidParams, "wall #{wall['id'].inspect} already exists on #{storey}; ids are unique per storey"
        end

        plan['walls'] << wall
        _, units, state = rebuild(model, plan, storey)
        units << raise_unit(model, state, storey)
        UnitJob.new('Plomada: add wall', units) do
          summary(state, storey).merge('wall' => wall['id'], 'walls' => plan['walls'].size)
        end
      end

      def add_opening(params, ctx)
        model = ctx.model
        storey = Storeys.resolve(model, params['storey'])
        plan = current_plan(model, storey)
        rec = params.reject { |k, _| PARAMS_NOT_RECORD.include?(k) }
        if plan['openings'].any? { |o| o['id'] == rec['id'] }
          raise InvalidParams, "opening #{rec['id'].inspect} already exists on #{storey}; ids are unique per storey"
        end

        plan['openings'] << rec
        norm, units, state = rebuild(model, plan, storey)
        stored = norm['openings'].find { |o| o['id'] == rec['id'] }
        units << ["opening #{rec['id']}", lambda {
          frames_of(state, rec['id']).each { |f| Openings.place(model, f.merge(record: Build.opening_record(stored))) }
        }]
        units << raise_unit(model, state, storey)
        UnitJob.new('Plomada: add opening', units) { summary(state, storey).merge('opening' => rec['id']) }
      end

      def move_opening(params, ctx)
        model = ctx.model
        storey = Storeys.resolve(model, params['storey'])
        id = Plan.text(params['id'], 'id')
        offset = Plan.non_negative(params['offset'], 'offset')
        plan = current_plan(model, storey)
        rec = plan['openings'].find { |o| o['id'] == id }
        raise InvalidParams, "id names opening #{id.inspect}, which is not on storey #{storey}" unless rec

        insts = SU.find_openings(model, id, storey)
        raise InvalidParams, "opening #{id} has a record but no component in the model; run build_plan again" if insts.empty?

        rec['offset'] = offset
        norm, units, state = rebuild(model, plan, storey)
        stored = norm['openings'].find { |o| o['id'] == id }
        units << ["opening #{id}", lambda {
          frames = frames_of(state, id).map { |f| f.merge(record: Build.opening_record(stored)) }
          if frames.size == 1 && insts.size == 1
            Openings.move(model, insts.first, frames.first, elevation: state[:elevation])
          else
            # Panels on a curve: their count and widths change with the offset, so they are placed anew.
            model.entities.erase_entities(insts.select(&:valid?))
            frames.each { |f| Openings.place(model, f) }
          end
        }]
        units << raise_unit(model, state, storey)
        UnitJob.new('Plomada: move opening', units) do
          summary(state, storey).merge('opening' => id, 'offset' => offset,
                                       'origin_mm' => frame_of(state, id)[:origin].map { |v| v.round(1) })
        end
      end

      def set_wall_height(params, ctx)
        model = ctx.model
        storey = Storeys.resolve(model, params['storey'])
        id = Plan.text(params['id'], 'id')
        height = Plan.positive(params['height'], 'height')
        plan = current_plan(model, storey)
        wall = plan['walls'].find { |w| w['id'] == id }
        raise InvalidParams, "id names wall #{id.inspect}, which is not on storey #{storey}" unless wall

        wall['height'] = height
        _, units, state = rebuild(model, plan, storey)
        units << raise_unit(model, state, storey)
        UnitJob.new('Plomada: set wall height', units) { summary(state, storey).merge('wall' => id, 'height' => height) }
      end

      SLAB_FIELDS = %w[id outline holes thickness top_offset material edge_band].freeze

      # add_slab: the 0.4 call (thickness and a straight outline, or none)
      # still builds the one automatic slab. With an id, holes, a top offset,
      # a material, an edge band or a bulged outline, or on a storey that
      # already has slab records, it adds a slab record and rebuilds the
      # storey's slabs from all of them.
      def add_slab(params, ctx)
        model = ctx.model
        storey = Storeys.resolve(model, params['storey'])
        existing = Slabs.stored_records(model, storey)
        bulged = params['outline'].is_a?(Array) && params['outline'].any? { |p| p.is_a?(Array) && p.size == 3 }
        return slab_04(params, ctx, storey) unless existing.any? || bulged || (params.keys & (SLAB_FIELDS - %w[outline thickness])).any?

        settings = PlanReader.storey_settings(model, storey)
        raw = params.slice(*SLAB_FIELDS)
        raw['id'] ||= next_slab_id(existing)
        if existing.any? { |r| r['id'] == raw['id'] }
          raise InvalidParams, "slab #{raw['id'].inspect} already exists on #{storey}; change it with set_slab"
        end

        raw['outline'] ||= exterior(model, settings, storey)[:points]
        rec = Plan.slab(raw, 'slab')
        rebuild_slabs(model, storey, settings, existing + [rec], 'Plomada: add slab', rec['id'])
      end

      # set_slab: changes the given fields of one slab record and rebuilds the
      # storey's slabs.
      def set_slab(params, ctx)
        model = ctx.model
        storey = Storeys.resolve(model, params['storey'])
        id = Plan.text(params['id'], 'id')
        existing = Slabs.stored_records(model, storey)
        old = existing.find { |r| r['id'] == id }
        unless old
          ids = existing.map { |r| r['id'] }
          raise InvalidParams, "id names slab #{id.inspect}, which is not on storey #{storey}; " \
                               "its slab records are #{ids.empty? ? 'none (add_slab with an id makes one)' : ids.join(', ')}"
        end

        settings = PlanReader.storey_settings(model, storey)
        rec = Plan.slab(old.reject { |k, _| %w[v kind].include?(k) }.merge(params.slice(*SLAB_FIELDS)), 'slab')
        records = existing.map { |r| r['id'] == id ? rec : r }
        rebuild_slabs(model, storey, settings, records, 'Plomada: set slab', id)
      end

      def next_slab_id(existing)
        n = 1
        n += 1 while existing.any? { |r| r['id'] == "L#{n}" }
        "L#{n}"
      end

      def rebuild_slabs(model, storey, settings, records, label, id)
        records = records.map { |r| Plan.slab(r.reject { |k, _| %w[v kind].include?(k) }, "slab #{r['id']}") }
        elevation = settings.fetch('elevation', 0.0).to_f
        wells = Storeys.wells(model, elevation, storey)
        lay = Geometry.slab_layout(records, wells)
        old = Storeys.entities(model, storey).select { |g| %w[slab slab_band].include?(SU.kind(g)) }
        state = { elevation: elevation, warnings: lay[:warnings].dup }
        units = [['floor slabs', lambda {
          SU.ensure_materials(model)
          SU.ensure_tags(model)
          model.entities.erase_entities(old.select(&:valid?)) unless old.empty?
          state[:before] = Storeys.snapshot(model)
          state[:groups] = Slabs.records(model, storey, lay, records, state[:warnings])
        }], raise_unit(model, state, storey)]
        UnitJob.new(label, units) do
          { 'storey' => storey, 'slab' => id, 'slabs' => records.map { |r| r['id'] }, 'groups' => state[:groups],
            'stair_wells' => wells.size, 'warnings' => state[:warnings] }
        end
      end

      def slab_04(params, ctx, storey)
        model = ctx.model
        settings = PlanReader.storey_settings(model, storey)
        elevation = settings.fetch('elevation', 0.0).to_f
        thickness = params['thickness'].nil? ? CONFIG[:slab_thickness_mm] : Plan.positive(params['thickness'], 'thickness')
        outline = if params['outline']
                    pts = params['outline']
                    raise InvalidParams, 'outline must be a list of at least 3 [x, y] points' unless pts.is_a?(Array) && pts.size >= 3

                    poly = pts.each_with_index.map { |p, i| Plan.point(p, "outline[#{i}]") }
                    Geometry.signed_area(poly).negative? ? poly.reverse : poly
                  else
                    exterior(model, settings, storey)[:points]
                  end
        wells = Storeys.wells(model, elevation, storey)
        old = Storeys.entities(model, storey).select { |g| SU.kind(g) == 'slab' }
        state = { elevation: elevation }
        units = [['floor slab', lambda {
          SU.ensure_materials(model)
          SU.ensure_tags(model)
          model.entities.erase_entities(old.select(&:valid?)) unless old.empty?
          state[:before] = Storeys.snapshot(model)
          Slabs.slab(model, storey, outline, thickness, holes: wells)
        }], raise_unit(model, state, storey)]
        UnitJob.new('Plomada: add slab', units) do
          { 'storey' => storey, 'group' => SU.group_name(storey, 'losa'), 'thickness' => thickness, 'outline' => outline,
            'stair_wells' => wells.size }
        end
      end

      def add_roof(params, ctx)
        model = ctx.model
        storey = Storeys.resolve(model, params['storey'])
        settings = PlanReader.storey_settings(model, storey)
        kind = Plan.choice(params.fetch('kind', 'flat') || 'flat', 'kind', %w[flat gable hip])
        overhang = params['overhang'].nil? ? CONFIG[:roof_overhang_mm] : Plan.non_negative(params['overhang'], 'overhang')
        thickness = params['thickness'].nil? ? CONFIG[:roof_thickness_mm] : Plan.positive(params['thickness'], 'thickness')
        pitch = params['pitch'].nil? ? CONFIG[:gable_pitch_deg] : Plan.positive(params['pitch'], 'pitch')
        raise InvalidParams, "pitch must be less than 75, got #{Plan.fmt(pitch)}" if pitch >= 75

        ext = exterior(model, settings, storey)
        height = settings['height'].to_f
        pitched = Build.pitched_roof(kind, ext, height, 'overhang' => overhang, 'pitch' => pitch, 'roof_thickness' => thickness)
        old = Storeys.entities(model, storey).select { |g| SU.kind(g) == 'roof' }
        state = { elevation: settings.fetch('elevation', 0.0).to_f }
        units = [["#{kind} roof", lambda {
          SU.ensure_materials(model)
          SU.ensure_tags(model)
          model.entities.erase_entities(old.select(&:valid?)) unless old.empty?
          state[:before] = Storeys.snapshot(model)
          if pitched
            Slabs.pitched_roof(model, storey, pitched, kind, thickness, overhang, pitch)
          else
            Slabs.flat_roof(model, storey, ext[:points], height, thickness, overhang)
          end
        }], raise_unit(model, state, storey)]
        UnitJob.new('Plomada: add roof', units) do
          { 'storey' => storey, 'group' => SU.group_name(storey, 'techo'), 'kind' => kind, 'overhang' => overhang,
            'thickness' => thickness, 'pitch' => kind == 'flat' ? nil : pitch }
        end
      end

      # A lawn around the buildings: the plan box of everything Plomada built
      # grown by +margin+, its top at the underside of the lowest storey's slab,
      # holes under the outer faces of that storey's buildings. Replaces the
      # previous terrain.
      def add_terrain(params, ctx)
        model = ctx.model
        margin = params['margin'].nil? ? CONFIG[:terrain_margin_mm] : Plan.positive(params['margin'], 'margin')
        thickness = params['thickness'].nil? ? CONFIG[:terrain_thickness_mm] : Plan.positive(params['thickness'], 'thickness')
        material = params['material'].nil? ? 'MAT_cesped' : Plan.text(params['material'], 'material')
        unless CONFIG[:materials].key?(material)
          raise InvalidParams, "material #{material.inspect} is not a Plomada material; use one of #{CONFIG[:materials].keys.join(', ')}"
        end

        storeys = Storeys.list(model)
        lowest = storeys.first
        raise InvalidParams, 'no Plomada walls in this model; build a house before add_terrain' unless lowest

        ground_level = Storeys.ground_level(model)
        sunk = Geometry::Levels.basements(storeys, ground_level)
        # 0.4: no ground level stored and nothing below 0 -> the lawn's top is the underside of the lowest slab.
        legacy = !Storeys.ground_level_stored?(model) && sunk.empty?
        settings = PlanReader.storey_settings(model, lowest['name'])
        through = legacy ? [lowest] : storeys.select { |s| s['elevation'] <= ground_level + CONFIG[:site_snap_mm] }
        holes = through.flat_map { |s| storey_exteriors(model, s['name']) } + Site.pool_outlines(model)
        holes += Ramps.outlines(model, through.map { |s| s['name'] }) unless legacy
        top = if legacy
                lowest['elevation'] - (settings['slab'] == false ? 0.0 : settings.fetch('slab_thickness', CONFIG[:slab_thickness_mm]).to_f)
              else
                ground_level
              end
        bb = Geom::BoundingBox.new
        SU.plomada_entities(model).each { |e| bb.add(e.bounds) unless %w[terrain room].include?(SU.kind(e)) }
        lo = SU.to_mm(bb.min)
        hi = SU.to_mm(bb.max)
        outer = [[lo[0] - margin, lo[1] - margin], [hi[0] + margin, lo[1] - margin],
                 [hi[0] + margin, hi[1] + margin], [lo[0] - margin, hi[1] + margin]]
        old = model.entities.grep(Sketchup::Group).select { |g| g.valid? && SU.kind(g) == 'terrain' }
        units = [['terrain', lambda {
          SU.ensure_materials(model)
          SU.ensure_tags(model)
          model.entities.erase_entities(old.select(&:valid?)) unless old.empty?
          if legacy
            Terrain.build(model, outer, holes, top, thickness, material)
          else
            Terrain.build_region(model, Geometry::Boolean.region([{ outer: outer, holes: [] }], holes), top, thickness,
                                 material, holes.size)
          end
        }]]
        UnitJob.new('Plomada: add terrain', units) do
          { 'group' => 'Terreno', 'material' => material, 'top_mm' => top, 'holes' => holes.size,
            'size_mm' => [(hi[0] - lo[0] + (2 * margin)).round(1), (hi[1] - lo[1] + (2 * margin)).round(1)] }
        end
      end

      # The outer faces of a storey's buildings (plan rings, mm).
      def storey_exteriors(model, storey)
        settings = PlanReader.storey_settings(model, storey)
        plan = Plan.normalize(current_plan(model, storey).merge('storey' => nil))
        layout = Geometry.solve_walls(plan['walls'], plan['openings'], storey_height: settings['height'].to_f)
        (layout[:exteriors] || []).map { |o| o[:points] }
      end

      def exterior(model, settings, storey)
        raw = current_plan(model, storey).merge('storey' => { 'name' => storey, 'height' => settings['height'] })
        plan = Plan.normalize(raw)
        layout = Geometry.solve_walls(plan['walls'], plan['openings'], storey_height: plan['storey']['height'])
        layout[:exterior] || raise(InvalidParams, 'the plan has no closed exterior wall to take the outline from; pass outline')
      end

      # Paints a wall (by id), an opening (by id), a named group or every
      # top-level entity on a tag. Wall and opening ids are looked up on
      # +storey+ when given, else on every storey (and must then be unique).
      def set_material(params, ctx)
        model = ctx.model
        target = Plan.text(params['target'], 'target')
        name = Plan.text(params['material'], 'material')
        storey = params['storey'].nil? ? nil : Storeys.resolve(model, params['storey'])
        mat = SU.material(model, name) if CONFIG[:materials].key?(name) || model.materials[name]
        raise InvalidParams, "material #{name.inspect} is not in the model; Plomada materials are #{CONFIG[:materials].keys.join(', ')}" unless mat || CONFIG[:materials].key?(name)

        groups = walls_groups(model).select { |g| storey.nil? || Storeys.of(g) == storey }
        hits = groups.select { |g| g.entities.grep(Sketchup::Face).any? { |f| f.get_attribute(DICT, 'wall') == target } }
        if hits.size > 1
          raise InvalidParams, "wall #{target.inspect} is on storeys #{hits.map { |g| Storeys.of(g) }.join(', ')}; pass storey"
        end

        group = hits.first
        opening = SU.find_opening(model, target, storey)
        named = model.entities.find { |e| (e.is_a?(Sketchup::Group) || e.is_a?(Sketchup::ComponentInstance)) && e.valid? && e.name == target }
        tagged = model.layers[target] ? model.entities.select { |e| e.valid? && e.layer.name == target } : []
        unless group || opening || named || !tagged.empty?
          raise InvalidParams, "target #{target.inspect} matches no wall id, opening id, group name or tag in the model"
        end

        units = [["paint #{target}", lambda {
          m = SU.material(model, name)
          if group
            apply_finishes(group, finishes(group).merge(target => name))
          elsif opening
            opening.material = m
          elsif named
            named.material = m
          else
            tagged.each { |e| e.material = m if e.respond_to?(:material=) }
          end
        }]]
        what = if group then 'wall' elsif opening then 'opening' elsif named then 'group' else 'tag' end
        UnitJob.new('Plomada: set material', units) do
          { 'target' => target, 'matched' => what, 'material' => name, 'count' => what == 'tag' ? tagged.size : 1 }
        end
      end

      # Erases every top-level group, instance and scene carrying a plomada
      # attribute. Never Sketchup.file_new: on Windows it can open a save
      # dialog that would freeze the pump.
      def reset(_params, ctx)
        model = ctx.model
        doomed = SU.plomada_entities(model)
        pages = model.pages.select { |p| p.attribute_dictionary(DICT) }
        units = [['erase Plomada objects', lambda {
          model.entities.erase_entities(doomed.select(&:valid?)) unless doomed.empty?
          pages.each { |p| model.pages.erase(p) }
        }]]
        UnitJob.new('Plomada: reset', units) { { 'erased' => doomed.size, 'scenes_erased' => pages.size } }
      end

      def undo(params, _ctx)
        steps = params['steps'].nil? ? 1 : params['steps']
        unless steps.is_a?(Integer) && steps.between?(1, CONFIG[:undo_max_steps])
          raise InvalidParams, "steps must be an integer from 1 to #{CONFIG[:undo_max_steps]}, got #{steps.inspect}"
        end

        units = [["undo #{steps}", -> { steps.times { Sketchup.undo } }]]
        UnitJob.new('Plomada: undo', units, operation: false) { { 'undone' => steps } }
      end
    end
  end
end
