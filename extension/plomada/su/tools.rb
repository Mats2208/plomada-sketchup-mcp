# frozen_string_literal: true

require 'json'
require 'fileutils'
require 'stringio'
require 'tmpdir'
require_relative 'kit'
require_relative 'build'
require_relative '../jobs'
require_relative '../clock'

module Plomada
  module SU
    # Read-only views of the model.
    module Inspect
      UNITS = %w[inches feet millimetres centimetres metres yards].freeze

      module_function

      def model_info(model, params = {})
        info = summary(model)
        info['detail'] = detail(model) if params['detail'] == true
        info
      end

      # What a check of the house needs: per walls group its manifold flag,
      # face count and the faces strip_internal_faces would still remove;
      # doors, windows and glass panes among the opening components.
      def detail(model)
        groups = model.entities.grep(Sketchup::Group).select { |g| g.valid? && SU.plomada?(g) && SU.kind(g) != 'room' }
        walls = groups.select { |g| SU.kind(g) == 'walls' }.map do |g|
          storey = Storeys.of(g)
          solids = storey_solids(model, storey)
          # The solver works at z 0: probe the storey's walls lowered back there.
          elevation = PlanReader.storey_settings(model, storey).fetch('elevation', 0.0).to_f
          down = Geom::Transformation.translation(Geom::Vector3d.new(0, 0, -elevation.mm))
          internal = solids ? Walls.internal_faces(g.entities, down * g.transformation, solids, CONFIG[:probe_mm]).size : nil
          { 'name' => g.name, 'storey' => storey, 'manifold' => g.manifold?,
            'faces' => g.entities.grep(Sketchup::Face).size, 'internal_faces' => internal }
        end
        openings = model.entities.grep(Sketchup::ComponentInstance).select { |i| i.valid? && SU.kind(i) == 'opening' }
        panes = openings.sum { |i| i.definition.entities.grep(Sketchup::Group).count { |p| p.name == 'vidrio' } }
        {
          'groups' => groups.map(&:name).sort, 'walls' => walls,
          'doors' => openings.count { |i| i.get_attribute(DICT, 'opening_kind') == 'door' },
          'windows' => openings.count { |i| i.get_attribute(DICT, 'opening_kind') == 'window' },
          'opening_names' => openings.map(&:name).sort, 'glass_panes' => panes,
          'rooms' => model.entities.grep(Sketchup::Group).count { |g| g.valid? && SU.kind(g) == 'room' },
          'stairs' => groups.count { |g| SU.kind(g) == 'stair' }, 'storeys' => Storeys.list(model)
        }
      end

      def storey_solids(model, storey)
        plan = Plan.normalize(PlanReader.read(model, storey).merge('storey' => nil))
        settings = PlanReader.storey_settings(model, storey)
        height = settings['height'].to_f
        Geometry.solve_walls(plan['walls'], Geometry.wall_openings(plan, height), storey_height: height)[:solids]
      rescue Plomada::Error
        nil
      end

      def summary(model)
        counts = Hash.new(0)
        types = Hash.new(0)
        model.entities.each do |e|
          counts[e.layer.name] += 1 if e.respond_to?(:layer)
          types[e.typename] += 1
        end
        plomada = Hash.new(0)
        SU.plomada_entities(model).each { |e| plomada[SU.kind(e)] += 1 }
        unit = model.options['UnitsOptions']['LengthUnit']
        {
          'path' => model.path, 'title' => model.title, 'units' => UNITS[unit] || unit.to_s,
          'modified' => model.modified?, 'counts_per_tag' => counts, 'counts_per_type' => types,
          'bounding_box_mm' => SU.bounds_mm(model.bounds), 'plomada' => plomada,
          'scenes' => model.pages.size, 'definitions' => model.definitions.size, 'materials' => model.materials.size
        }
      end

      def list_entities(model, params)
        tag = params['tag']
        limit = params['limit'].nil? ? CONFIG[:list_page_size] : params['limit']
        unless limit.is_a?(Integer) && limit.between?(1, CONFIG[:list_page_size])
          raise InvalidParams, "limit must be an integer from 1 to #{CONFIG[:list_page_size]}, got #{limit.inspect}"
        end

        cursor = params['cursor']
        raise InvalidParams, "cursor must be an integer persistent_id, got #{cursor.inspect}" unless cursor.nil? || cursor.is_a?(Integer)

        ents = model.entities.to_a
        ents.select! { |e| e.respond_to?(:layer) && e.layer.name == tag } if tag
        ents.sort_by!(&:persistent_id)
        ents.reject! { |e| e.persistent_id <= cursor } if cursor
        page = ents.first(limit)
        {
          'items' => page.map { |e| entity_row(e) },
          'next_cursor' => ents.size > limit ? page.last.persistent_id : nil,
          'count' => page.size
        }
      end

      def entity_row(e)
        row = { 'persistent_id' => e.persistent_id, 'type' => e.typename,
                'tag' => e.respond_to?(:layer) ? e.layer.name : nil }
        row['name'] = e.name if e.respond_to?(:name)
        row['definition'] = e.definition.name if e.is_a?(Sketchup::ComponentInstance)
        row['material'] = e.material&.name if e.respond_to?(:material)
        if SU.plomada?(e)
          row['plomada'] = { 'kind' => SU.kind(e), 'id' => e.get_attribute(DICT, 'id') }.compact
        end
        row['bounds_mm'] = SU.bounds_mm(e.bounds) if e.respond_to?(:bounds)
        row
      end

      def list_tags(model)
        counts = Hash.new(0)
        model.entities.each { |e| counts[e.layer.name] += 1 if e.respond_to?(:layer) }
        {
          'tags' => model.layers.map do |l|
            { 'name' => l.name, 'visible' => l.visible?, 'color' => l.color.to_a.first(3),
              'entities' => counts[l.name], 'plomada' => CONFIG[:tags].include?(l.name) }
          end
        }
      end

      def list_materials(model)
        {
          'materials' => model.materials.map do |m|
            row = { 'name' => m.name, 'rgb' => m.color.to_a.first(3), 'alpha' => m.alpha.round(3),
                    'texture' => !m.texture.nil?, 'plomada' => CONFIG[:materials].key?(m.name) }
            row['metallic'] = m.metallic_factor.round(3) if m.respond_to?(:metalness_enabled?) && m.metalness_enabled?
            row['roughness'] = m.roughness_factor.round(3) if m.respond_to?(:roughness_enabled?) && m.roughness_enabled?
            row
          end
        }
      end
    end

    # Viewport capture: a JPEG written by view.write_image, shrunk in 15
    # percent steps until it is under the byte cap. The camera and the render
    # mode are put back in ensure, whatever happens.
    module Capture
      STYLES = { 'shaded' => 2, 'hidden_line' => 1, 'lines_only' => 0 }.freeze

      module_function

      def style!(value, path = 'style', allowed = STYLES.keys)
        Plan.choice(value, path, allowed)
      end

      def dims(params, cfg_w, cfg_h)
        w = params['width'].nil? ? cfg_w : params['width']
        h = params['height'].nil? ? cfg_h : params['height']
        [[w, 'width'], [h, 'height']].each do |v, name|
          raise InvalidParams, "#{name} must be an integer from 64 to 8192 px, got #{v.inspect}" unless v.is_a?(Integer) && v.between?(64, 8192)
        end
        [w, h]
      end

      def capture(model, params)
        width, height = dims(params, CONFIG[:capture_width], CONFIG[:capture_height])
        style = style!(params.fetch('style', 'shaded') || 'shaded')
        framing = Plan.choice(params.fetch('view', 'current') || 'current', 'view', %w[current fit])
        view = model.active_view
        raise Error.new(Codes::SKETCHUP, 'the SketchUp window is minimized; restore it and retry') if view.vpwidth.to_i < 1

        t0 = Clock.now_ms
        with_view(model, STYLES[style], framing == 'fit') do
          path = next_path
          w = width
          h = height
          attempts = 0
          loop do
            attempts += 1
            ok = write(view, path, w, h, CONFIG[:capture_antialias], CONFIG[:capture_jpeg_quality])
            raise Error.new(Codes::SKETCHUP, "view.write_image returned false for #{path}") unless ok

            size = File.size(path)
            break if size <= CONFIG[:capture_max_bytes]
            break if [w, h].min * CONFIG[:capture_shrink] < CONFIG[:capture_min_side]

            w = (w * CONFIG[:capture_shrink]).round
            h = (h * CONFIG[:capture_shrink]).round
          end
          data = File.binread(path)
          { 'path' => path, 'width' => w, 'height' => h, 'bytes' => data.bytesize, 'mime' => 'image/jpeg',
            'style' => style, 'attempts' => attempts, 'capture_ms' => (Clock.now_ms - t0).round(1),
            'data_base64' => [data].pack('m0') }
        end
      end

      def write(view, path, w, h, antialias, quality)
        view.write_image(filename: path, width: w, height: h, antialias: antialias, compression: quality, transparent: false)
      rescue ArgumentError, TypeError
        view.write_image(path, w, h, antialias, quality)
      end

      # Runs the block with a render mode and (optionally) a fitted camera,
      # then puts both back.
      def with_view(model, render_mode, fit)
        view = model.active_view
        ro = model.rendering_options
        old_mode = ro['RenderMode']
        cam = view.camera
        saved = { eye: cam.eye, target: cam.target, up: cam.up, persp: cam.perspective?,
                  fov: cam.perspective? ? cam.fov : nil, height: cam.perspective? ? nil : cam.height }
        begin
          ro['RenderMode'] = render_mode if render_mode && old_mode != render_mode
          View.fit(model) if fit
          yield
        ensure
          ro['RenderMode'] = old_mode if render_mode && ro['RenderMode'] != old_mode
          if fit
            restored = Sketchup::Camera.new(saved[:eye], saved[:target], saved[:up], saved[:persp], saved[:fov] || 35.0)
            restored.height = saved[:height] if saved[:height]
            view.camera = restored
          end
        end
      end

      def next_path
        dir = (Sketchup.temp_dir || ENV['TEMP'] || Dir.tmpdir).tr('\\', '/')
        old = Dir.glob(File.join(dir, 'plomada_capture_*.jpg')).sort
        old.first([old.size - CONFIG[:captures_kept] + 1, 0].max).each do |f|
          File.delete(f)
        rescue SystemCallError
          nil
        end
        File.join(dir, "plomada_capture_#{Time.now.strftime('%Y%m%d_%H%M%S_%L')}.jpg")
      end
    end

    # Scenes with a level two-point camera at eye height, and their images.
    module Scenes
      STYLES = { 'shaded' => 2, 'hidden_line' => 1, 'lines_only' => 0 }.freeze
      AUTO_HIDDEN_TAGS = %w[Ambientes].freeze # room labels: plan annotation, not part of a render

      module_function

      def create(params, ctx)
        model = ctx.model
        name = Plan.text(params['name'], 'name')
        eye_xy = Plan.point(params['eye'], 'eye')
        eye_h = params['eye_height'].nil? ? CONFIG[:eye_height_mm] : Plan.number(params['eye_height'], 'eye_height')
        tgt = params['target']
        unless tgt.is_a?(Array) && [2, 3].include?(tgt.size)
          raise InvalidParams, "target must be [x, y, z] in mm, got #{tgt.inspect}"
        end

        tgt = tgt.each_with_index.map { |v, i| Plan.number(v, "target[#{i}]") }
        fov = params['fov'].nil? ? CONFIG[:scene_fov_deg] : Plan.positive(params['fov'], 'fov', 179.0)
        two_point = params.fetch('two_point', true) != false
        style = Capture.style!(params.fetch('style', 'shaded') || 'shaded', 'style', STYLES.keys)
        eye = [eye_xy[0], eye_xy[1], eye_h]
        target = [tgt[0], tgt[1], two_point ? eye_h : (tgt[2] || eye_h)]
        if Geometry.dist(eye, target) < 1.0 && (eye[2] - target[2]).abs < 1.0
          raise InvalidParams, 'eye and target are the same point; move the target'
        end

        units = [["scene #{name}", -> { add_page(model, name, eye, target, fov, style, two_point) }]]
        UnitJob.new('Plomada: create scene', units) do
          { 'scene' => name, 'eye_mm' => eye, 'target_mm' => target, 'fov' => fov, 'two_point' => two_point,
            'style' => style, 'scenes' => model.pages.size }
        end
      end

      # Parallel 45-degree scenes for documentation and AI: AX_general (the whole
      # building) and AX_<storey> cut cut_height above each floor, the section
      # plane stored in the scene. Each is framed on the building, then the site
      # comes back around it.
      def axonometric(params, ctx)
        model = ctx.model
        storeys = Storeys.list(model)
        raise InvalidParams, 'no Plomada walls in this model; build a house before axonometric_scenes' if storeys.empty?

        cut = params['cut_height'].nil? ? 1200.0 : Plan.positive(params['cut_height'], 'cut_height', 10_000.0)
        style = Capture.style!(params.fetch('style', 'shaded') || 'shaded', 'style', STYLES.keys)
        bb = Geom::BoundingBox.new
        SU.plomada_entities(model).each { |e| bb.add(e.bounds) unless %w[site terrain room].include?(SU.kind(e)) }
        lo, hi = SU.to_mm(bb.min), SU.to_mm(bb.max)
        box = [lo[0, 2], hi[0, 2]]
        specs = [['AX_general', nil]] + storeys.map { |s| ["AX_#{s['name']}", s['elevation'] + cut] }
        made = []
        units = specs.map do |name, cut_z|
          ["scene #{name}", lambda {
            v = Geometry.axonometric_drawing(name, box, 3000, 2000, cut_z: cut_z)
            model.entities.grep(Sketchup::SectionPlane)
                 .select { |sp| sp.get_attribute(DICT, 'kind') == 'axonometric_cut' && sp.get_attribute(DICT, 'scene') == name }
                 .each(&:erase!)
            plane = nil
            if v[:section]
              plane = model.entities.add_section_plane([SU.pt(v[:section][:point]), Geom::Vector3d.new(*v[:section][:normal])])
              plane.name = name if plane.respond_to?(:name=)
              SU.set_attrs(plane, { 'kind' => 'axonometric_cut', 'scene' => name })
            end
            model.entities.active_section_plane = plane
            view = model.active_view
            cam = Sketchup::Camera.new(SU.pt(v[:eye]), SU.pt(v[:target]), Z_AXIS)
            cam.perspective = false
            view.camera = cam
            around = model.entities.select { |e| (e.is_a?(Sketchup::Group) || e.is_a?(Sketchup::ComponentInstance)) && e.visible? && %w[site terrain].include?(SU.kind(e)) }
            around.each { |e| e.visible = false }
            view.zoom_extents
            around.each { |e| e.visible = true }
            view.camera.height = view.camera.height * 1.3 # room for an export at another aspect ratio
            model.rendering_options['RenderMode'] = STYLES[style]
            model.rendering_options['DisplaySectionPlanes'] = false
            page = model.pages[name] || model.pages.add(name)
            page.transition_time = 0.0 if page.respond_to?(:transition_time=)
            page.use_section_planes = true
            labels = [model.layers['Ambientes']].compact.select(&:visible?) # room labels stay out of renders
            labels.each { |l| l.visible = false }
            page.update
            labels.each { |l| l.visible = true }
            SU.set_attrs(page, { 'kind' => 'scene', 'style' => style, 'view' => 'axonometric', 'cut_z' => cut_z })
            model.entities.active_section_plane = nil
            made << { 'name' => name, 'cut_z_mm' => cut_z }
          }]
        end
        UnitJob.new('Plomada: axonometric scenes', units) do
          { 'scenes' => made, 'total_scenes' => model.pages.size }
        end
      end

      # Sets the camera and saves it as scene +name+ (created or updated).
      # +hidden_tags+ are hidden in this scene only (the room labels, for renders).
      def add_page(model, name, eye, target, fov, style, two_point, attrs = {}, hidden_tags: [])
        model.active_view.camera = Sketchup::Camera.new(SU.pt(eye), SU.pt(target), Z_AXIS, true, fov)
        model.rendering_options['RenderMode'] = STYLES[style]
        page = model.pages[name] || model.pages.add(name)
        page.transition_time = 0.0 if page.respond_to?(:transition_time=)
        # The scene stores "no section cut", so an axonometric scene's cut never carries over into it.
        model.entities.active_section_plane = nil
        page.use_section_planes = true
        hidden = hidden_tags.filter_map { |t| model.layers[t] }.select(&:visible?)
        hidden.each { |l| l.visible = false }
        page.update
        hidden.each { |l| l.visible = true }
        SU.set_attrs(page, { 'kind' => 'scene', 'style' => style, 'eye' => JSON.generate(eye),
                             'target' => JSON.generate(target), 'two_point' => two_point }.merge(attrs))
        model.pages.selected_page = page
        page
      end

      # Scenes made without coordinates: one interior per room (I_N00_01_Living),
      # eye height above its storey's floor, plus four eye-level exteriors
      # (E1_suroeste ...) and one aerial (A_aerea) around everything Plomada
      # built. replace first erases the scenes an earlier auto_scenes made.
      def auto(params, ctx)
        model = ctx.model
        style = Capture.style!(params.fetch('style', 'shaded') || 'shaded', 'style', STYLES.keys)
        interior = params.fetch('interior', true) != false
        exterior = params.fetch('exterior', true) != false
        fov_in = params['interior_fov'].nil? ? CONFIG[:interior_fov_deg] : Plan.positive(params['interior_fov'], 'interior_fov', 120.0)
        fov_out = params['exterior_fov'].nil? ? CONFIG[:exterior_fov_deg] : Plan.positive(params['exterior_fov'], 'exterior_fov', 120.0)
        eye_h = CONFIG[:eye_height_mm]
        storeys = params['storey'].nil? ? Storeys.list(model).map { |s| s['name'] } : [Storeys.resolve(model, params['storey'])]
        raise InvalidParams, 'no Plomada walls in this model; build a house before auto_scenes' if storeys.empty?

        cams = []
        skipped = []
        if interior
          storeys.each do |storey|
            plan = Plan.normalize(PlanReader.read(model, storey).merge('storey' => nil))
            elevation = PlanReader.storey_settings(model, storey).fetch('elevation', 0.0).to_f
            # The stairs of this storey and the wells of the ones coming up block the eye like walls.
            stairs = Storeys.entities(model, storey).select { |e| SU.kind(e) == 'stair' }
                            .map { |g| JSON.parse(g.get_attribute(DICT, 'footprint')) }
            edges = Geometry.wall_body_edges(plan['walls']) +
                    Geometry.ring_edges(stairs + Storeys.wells(model, elevation, storey))
            axis = Geometry.dominant_axis(plan['walls'])
            lights = Geometry.light_openings(plan)
            pieces =plan['furniture'].select { |f| Geometry.furniture_item?(f['item']) }
                                      .map { |f| Geometry.furniture_footprint(f['item'], f['at'], f['rotation']) }
            plan['rooms'].each do |room|
              # Furniture is in the way too, except a piece the room point itself falls on.
              around = pieces.reject { |fp| Geometry.point_in_polygon?(room['at'], fp) }
              cam = Geometry.room_camera(room['at'], edges + Geometry.ring_edges(around), axis, lights: lights)
              next skipped << "#{storey}/#{room['id']}" unless cam

              z = elevation + eye_h
              cams << { name: scene_name('I', storey, room['number'] || room['id'], room['name']),
                        eye: cam[:eye] + [z], target: cam[:target] + [z], fov: fov_in, view: 'interior',
                        room: room['id'], storey: storey }
            end
          end
        end
        if exterior
          points = []
          SU.plomada_entities(model).each do |e|
            # The buildings only: the site around them (paving, fences, pools) is not framed.
            next unless e.is_a?(Sketchup::Group) && !%w[room terrain site].include?(SU.kind(e))

            world_points(e.entities, e.transformation, points)
          end
          fences = model.entities.grep(Sketchup::Group)
                        .select { |g| g.valid? && SU.kind(g) == 'site' && g.get_attribute(DICT, 'site_kind') == 'fence' }
                        .map { |g| JSON.parse(g.get_attribute(DICT, 'record')).then { |r| { points: r['points'], closed: r['closed'] } } }
          # A car is in the way as a whole; a tree only by its trunk, the eye may stand under the leaves.
          # A part picked from a library file can come in as a Group rather than a component instance.
          placed = model.entities.select do |e|
            (e.is_a?(Sketchup::ComponentInstance) || e.is_a?(Sketchup::Group)) && SU.kind(e) == 'site' &&
              e.get_attribute(DICT, 'site_kind') == 'object'
          end
          boxes = placed.map do |i|
            lo = SU.to_mm(i.bounds.min)[0, 2]
            hi = SU.to_mm(i.bounds.max)[0, 2]
            next [lo, hi] unless Site::TREES.include?(i.get_attribute(DICT, 'item').to_s.upcase)

            c = [(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0]
            [[c[0] - 300.0, c[1] - 300.0], [c[0] + 300.0, c[1] + 300.0]]
          end
          ext = Geometry.exterior_cameras(points.uniq, eye_h, fov_out)
          Geometry.clear_view(ext, fences, boxes).each do |c|
            cams << c.merge(fov: fov_out, view: 'exterior')
          end
          cams << { name: 'A_aerea', view: 'aerial', fov: CONFIG[:scene_fov_deg] }
        end
        old = params.fetch('replace', true) == false ? [] : model.pages.select { |pg| pg.get_attribute(DICT, 'auto') }
        units = [['erase earlier auto scenes', -> { old.each { |pg| model.pages.erase(pg) } }]]
        cams.each do |c|
          units << ["scene #{c[:name]}", lambda {
            if c[:view] == 'aerial'
              View.fit(model)
              cam = model.active_view.camera
              c[:eye] = SU.to_mm(cam.eye)
              c[:target] = SU.to_mm(cam.target)
              add_page(model, c[:name], c[:eye], c[:target], c[:fov], style, false, { 'auto' => true, 'view' => 'aerial' },
                       hidden_tags: AUTO_HIDDEN_TAGS)
            else
              add_page(model, c[:name], c[:eye], c[:target], c[:fov], style, true,
                       { 'auto' => true, 'view' => c[:view], 'room' => c[:room], 'storey' => c[:storey] },
                       hidden_tags: AUTO_HIDDEN_TAGS)
            end
          }]
        end
        UnitJob.new('Plomada: auto scenes', units) do
          { 'scenes' => cams.map { |c| { 'name' => c[:name], 'view' => c[:view], 'eye_mm' => c[:eye]&.map { |v| v.round(1) },
                                         'target_mm' => c[:target]&.map { |v| v.round(1) }, 'fov' => c[:fov] } },
            'rooms_skipped' => skipped, 'erased' => old.size, 'total_scenes' => model.pages.size }
        end
      end

      # Every vertex under +ents+ in world mm, rounded to 0.1 mm (so uniq merges them).
      def world_points(ents, tr, out)
        ents.each do |e|
          if e.is_a?(Sketchup::Edge)
            e.vertices.each { |v| out << SU.to_mm(v.position.transform(tr)).map { |x| x.round(1) } }
          elsif e.is_a?(Sketchup::Group)
            world_points(e.entities, tr * e.transformation, out)
          end
        end
        out
      end

      def scene_name(prefix, *parts)
        ([prefix] + parts.compact.map { |x| x.to_s.strip.gsub(/\s+/, '_') }).reject(&:empty?).join('_')
      end

      # One image per scene, one scene per step; camera and render mode are
      # restored by the last step (or on abort).
      def export_images(params, ctx)
        model = ctx.model
        dir = Plan.text(params['dir'], 'dir').tr('\\', '/')
        width, height = Capture.dims(params, CONFIG[:export_width], CONFIG[:export_height])
        style = params['style'].nil? ? nil : Capture.style!(params['style'])
        format = Plan.choice(params.fetch('format', 'png') || 'png', 'format', %w[png jpg])
        pages = model.pages.to_a
        raise InvalidParams, 'the model has no scenes; create one with create_scene first' if pages.empty?

        view = model.active_view
        ro = model.rendering_options
        cam = view.camera
        saved = [Sketchup::Camera.new(cam.eye, cam.target, cam.up, cam.perspective?, cam.perspective? ? cam.fov : 35.0), ro['RenderMode']]
        files = []
        cuts = model.entities.grep(Sketchup::SectionPlane).select { |sp| sp.get_attribute(DICT, 'kind') == 'axonometric_cut' }
        saved_plane = model.entities.active_section_plane
        saved_display = ro['DisplaySectionPlanes']
        # Room labels and the template's scale figure never belong in an image; they come back afterwards.
        labels = [model.layers['Ambientes']].compact.select(&:visible?)
        figures = SU.stray_figures(model)
        restore = lambda {
          view.camera = saved[0]
          ro['RenderMode'] = saved[1] if ro['RenderMode'] != saved[1]
          model.entities.active_section_plane = saved_plane&.valid? ? saved_plane : nil
          ro['DisplaySectionPlanes'] = saved_display
          labels.each { |l| l.visible = true if l.valid? }
          figures.each { |f| f.visible = true if f.valid? }
        }
        units = [['prepare', lambda {
          FileUtils.mkdir_p(dir)
          labels.each { |l| l.visible = false }
          figures.each { |f| f.visible = false }
        }]]
        pages.each do |page|
          units << ["image #{page.name}", lambda {
            view.camera = page.camera
            # The camera alone does not bring the scene's cut: an axonometric scene's own plane, none elsewhere.
            model.entities.active_section_plane = cuts.find { |sp| sp.valid? && sp.get_attribute(DICT, 'scene') == page.name }
            ro['DisplaySectionPlanes'] = false
            # The scene's own style (stored by create_scene) unless overridden;
            # a scene Plomada did not make keeps the current render mode.
            page_style = page.get_attribute(DICT, 'style')
            mode = Capture::STYLES[style || page_style] || saved[1]
            ro['RenderMode'] = mode if mode && ro['RenderMode'] != mode
            path = File.join(dir, "#{page.name.gsub(/[^\w\-. ]/, '_')}.#{format}")
            ok = Capture.write(view, path, width, height, true, 0.9)
            raise Error.new(Codes::SKETCHUP, "view.write_image returned false for #{path}") unless ok

            files << { 'scene' => page.name, 'path' => path, 'bytes' => File.size(path) }
          }]
        end
        units << ['restore view', restore]
        job = UnitJob.new('Plomada: export scene images', units, operation: false) do
          { 'dir' => dir, 'width' => width, 'height' => height, 'images' => files,
            'warnings' => SU.stray_figure_warnings(model).map { |w| "#{w} (hidden in these images)" } }
        end
        job.define_singleton_method(:on_abort) { |_e| restore.call }
        job
      end
    end

    module Export
      FORMATS = %w[skp fbx obj].freeze

      module_function

      def job(params, ctx)
        model = ctx.model
        format = Plan.choice(params['format'], 'format', FORMATS)
        path = Plan.text(params['path'], 'path').tr('\\', '/')
        path = "#{path.sub(/\.[A-Za-z0-9]+\z/, '')}.#{format}" unless path.downcase.end_with?(".#{format}")
        # save_copy refuses an untitled model, and Model#save can open the
        # modal "Purge Unused?" prompt that would freeze the pump.
        if format == 'skp' && model.path.to_s.empty?
          raise InvalidParams, 'the model has never been saved, so SketchUp cannot write a copy of it; save it once ' \
                               'in SketchUp (File > Save As), then call export_model again'
        end
        units = [["export #{format}", lambda {
          FileUtils.mkdir_p(File.dirname(path))
          ok = case format
               when 'skp' then model.save_copy(path)
               when 'fbx' then model.export(path, { show_summary: false })
               # One-sided faces: two-sided ones come into Blender as a second, reversed copy of every face.
               else model.export(path, { show_summary: false, triangulated_faces: true, doublesided_faces: false,
                                         edges: false, texture_maps: true })
               end
          unless ok
            raise Error.new(Codes::SKETCHUP, "export to #{format.upcase} returned false for #{path}; the " \
                                             "#{format.upcase} exporter may be missing from this SketchUp edition")
          end
          raise Error.new(Codes::SKETCHUP, "export to #{format.upcase} reported success but wrote no file at #{path}") unless File.file?(path)
        }]]
        UnitJob.new("Plomada: export #{format}", units, operation: false) do
          { 'path' => path, 'format' => format, 'bytes' => File.size(path), 'warnings' => SU.stray_figure_warnings(model) }
        end
      end
    end

    # execute_ruby: off unless allowed in the settings. A guard against
    # mistakes, not a sandbox.
    module RubyEval
      FORBIDDEN = /\b(?:system|exec|spawn|fork|exit!?|abort)\b|`|%x|\bThread\s*\.\s*new\b|\bSketchup\s*\.\s*quit\b/

      class SoftDeadline < StandardError; end

      # $stdout replacement that keeps at most +cap+ bytes.
      class CappedOutput < StringIO
        attr_reader :truncated

        def initialize(cap)
          super(+'')
          @cap = cap
          @truncated = false
        end

        def write(*parts)
          parts.each do |p|
            s = p.to_s
            room = @cap - string.bytesize
            if s.bytesize > room
              super(s.byteslice(0, [room, 0].max))
              @truncated = true
            else
              super(s)
            end
          end
          parts.sum { |p| p.to_s.bytesize }
        end
      end

      module_function

      def job(params, _ctx)
        code = params['code']
        raise InvalidParams, 'code must be a non-empty string' unless code.is_a?(String) && !code.strip.empty?
        if code.bytesize > CONFIG[:ruby_code_cap_bytes]
          raise InvalidParams, "code is #{code.bytesize} bytes; the limit is #{CONFIG[:ruby_code_cap_bytes]}"
        end
        if (hit = code.match(FORBIDDEN))
          raise InvalidParams, "code refused: it uses #{hit[0].strip.inspect}; execute_ruby blocks system, exec, spawn, " \
                               'fork, backticks, %x, Thread.new, exit and Sketchup.quit'
        end

        out = {}
        UnitJob.new('Plomada: execute_ruby', [['execute_ruby', -> { out.merge!(run(code)) }]]) { out }
      end

      # Runs the code with a soft deadline checked on every line (a
      # TracePoint, no threads): Ruby loops stop, native SketchUp calls run
      # to completion. Raising here aborts the step's operation.
      def run(code)
        deadline = Clock.now_ms + CONFIG[:ruby_soft_deadline_ms]
        capture = CappedOutput.new(CONFIG[:ruby_output_cap_bytes])
        tracer = TracePoint.new(:line) do
          raise SoftDeadline, "execute_ruby passed its #{CONFIG[:ruby_soft_deadline_ms] / 1000} s soft deadline" if Clock.now_ms > deadline
        end
        previous = $stdout
        t0 = Clock.now_ms
        begin
          $stdout = capture
          value = tracer.enable { Object.new.instance_eval { binding }.eval(code, '(execute_ruby)', 1) }
        rescue SoftDeadline => e
          raise Error.new(Codes::CANCELLED, "#{e.message}; the operation was aborted and nothing from this call remains")
        rescue NoMemoryError, SignalException
          raise
        rescue SystemExit
          raise Error.new(Codes::SKETCHUP, 'SystemExit: the code tried to exit; refused')
        rescue Exception => e # rubocop:disable Lint/RescueException
          raise Error.new(Codes::SKETCHUP, "#{e.class}: #{e.message}")
        ensure
          $stdout = previous
        end
        inspected = value.inspect
        cap = CONFIG[:ruby_output_cap_bytes]
        {
          'result' => inspected.bytesize > cap ? inspected.byteslice(0, cap) : inspected,
          'output' => capture.string, 'output_truncated' => capture.truncated,
          'elapsed_ms' => (Clock.now_ms - t0).round(1)
        }
      end
    end
  end
end
