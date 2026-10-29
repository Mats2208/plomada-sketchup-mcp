# frozen_string_literal: true

require 'fileutils'
require_relative 'kit'
require_relative 'builders'
require_relative 'storeys'
require_relative '../jobs'
require_relative '../geometry'

module Plomada
  module SU
    # The 2D drawings of the model as images: a plan cut per storey and one of
    # the whole plot, four elevations, sections and two axonometrics, each in a
    # textured ("render") and a black line ("linea") style, every orthographic
    # view at the same px per metre. One image per job step; the camera, the
    # render settings, what was hidden and the section planes are put back by
    # the last step (or on abort).
    module Drawings
      KINDS = %w[plans elevations sections axonometrics].freeze
      FIT_BUILDING_MARGIN = 1.1 # air around a building framed alone
      STYLES = %w[render line].freeze
      SKIP = %w[terrain room site].freeze

      module_function

      def job(params, ctx)
        model = ctx.model
        dir = Plan.text(params['dir'], 'dir').tr('\\', '/')
        kinds = list(params['views'], 'views', KINDS)
        styles = list(params['styles'], 'styles', STYLES)
        px_per_m = params['px_per_m'].nil? ? 110.0 : Plan.positive(params['px_per_m'], 'px_per_m', 400.0)
        cut = params['cut_height'].nil? ? 1200.0 : Plan.positive(params['cut_height'], 'cut_height', 10_000.0)
        storeys = Storeys.list(model)
        raise InvalidParams, 'no Plomada walls in this model; build a house before export_drawings' if storeys.empty?

        building = box_of(model) { |e| !SKIP.include?(SU.kind(e)) }
        plot = box_of(model) { |e| !%w[terrain room].include?(SU.kind(e)) }
        lowest = storeys.first
        settings = PlanReader.storey_settings(model, lowest['name'])
        ground = lowest['elevation'] - (settings['slab'] == false ? 0.0 : settings.fetch('slab_thickness', CONFIG[:slab_thickness_mm]).to_f)
        terrain = model.entities.grep(Sketchup::Group).find { |g| g.valid? && SU.kind(g) == 'terrain' }
        ground_cut = (terrain ? SU.to_mm(terrain.bounds.min)[2] : ground) - 10.0
        flat = ->(b) { [b[0][0, 2], b[1][0, 2]] }

        views = []
        if kinds.include?('plans')
          storeys.each do |s|
            views << Geometry.plan_drawing("planta_#{s['name']}", flat.(building), s['elevation'] + cut, px_per_m)
          end
          views << Geometry.plan_drawing('planta_lote', flat.(plot), lowest['elevation'] + cut, px_per_m)
        end
        if kinds.include?('elevations')
          Geometry.elevation_drawings(flat.(building), ground, building[1][2], ground_cut, px_per_m)
                  .each { |v| views << v.merge(hide_site: true) }
        end
        if kinds.include?('sections')
          specs = params['sections'].nil? || params['sections'].empty? ? Geometry.default_sections(flat.(building)) : params['sections']
          raise InvalidParams, 'sections must be a list of {name, axis, at, look}' unless specs.is_a?(Array)

          specs.each_with_index do |sp, i|
            raise InvalidParams, "sections[#{i}] must be an object {name, axis, at, look}" unless sp.is_a?(Hash)

            name = Plan.text(sp['name'], "sections[#{i}].name")
            axis = Plan.choice(sp['axis'], "sections[#{i}].axis", %w[x y])
            at = Plan.number(sp['at'], "sections[#{i}].at")
            look = Plan.choice(sp['look'], "sections[#{i}].look", Geometry::LOOKS.keys)
            views << Geometry.section_drawing("corte_#{Geometry.drawing_slug(name)}", axis, at, look, flat.(plot),
                                              plot[0][2], building[1][2], px_per_m)
          end
        end
        if kinds.include?('axonometrics')
          top = storeys.last
          views << Geometry.axonometric_drawing('axonometria', flat.(plot), 3000, 2000)
          views << Geometry.axonometric_drawing('axonometria_seccionada', flat.(plot), 3000, 2000,
                                                cut_z: top['elevation'] + top['height'] - 600.0)
          # One per storey, cut where its plan is cut and framed on the building, not the plot: the
          # 45-degree cutaway a set of drawings uses to show every floor's rooms and furniture.
          storeys.each do |s|
            views << Geometry.axonometric_drawing("axonometria_seccionada_#{s['name']}", flat.(building), 3000, 2000,
                                                  cut_z: s['elevation'] + cut).merge(fit_building: true)
          end
        end
        build_job(model, dir, views, styles, px_per_m)
      end

      def list(value, path, allowed)
        return allowed.dup if value.nil?
        raise InvalidParams, "#{path} must be a list drawn from #{allowed.join(', ')}" unless value.is_a?(Array) && !value.empty?

        value.map { |v| Plan.choice(v, path, allowed) }.uniq
      end

      # [[x0, y0, z0], [x1, y1, z1]] in mm of the top-level Plomada entities the block accepts.
      def box_of(model)
        bb = Geom::BoundingBox.new
        SU.plomada_entities(model).each { |e| bb.add(e.bounds) if yield(e) }
        raise InvalidParams, 'nothing to draw: the model has no Plomada geometry' if bb.empty?

        [SU.to_mm(bb.min), SU.to_mm(bb.max)]
      end

      def build_job(model, dir, views, styles, px_per_m)
        view = model.active_view
        ro = model.rendering_options
        cam = view.camera
        saved_cam = Sketchup::Camera.new(cam.eye, cam.target, cam.up, cam.perspective?, cam.perspective? ? cam.fov : 35.0)
        saved_cam.height = cam.height unless cam.perspective?
        keys = %w[RenderMode Texture EdgeColorMode ForegroundColor DrawSilhouettes SilhouetteWidth SectionCutFilled
                  SectionDefaultFillColor SectionCutWidth DisplaySectionPlanes DisplaySectionCuts DrawGround DrawHorizon
                  BackgroundColor DisplayText]
        saved_ro = keys.to_h { |k| [k, ro[k]] }
        saved_shadows = model.shadow_info['DisplayShadows']
        state = { hidden: [], plane: nil }
        files = []
        restore = lambda {
          unhide(state)
          drop_plane(model, state)
          saved_ro.each { |k, v| ro[k] = v unless v.nil? || ro[k] == v }
          model.shadow_info['DisplayShadows'] = saved_shadows
          view.camera = saved_cam
        }
        units = [['prepare', -> { FileUtils.mkdir_p(dir) }]]
        views.each do |v|
          styles.each do |style|
            units << ["#{v[:name]} #{style}", lambda {
              unhide(state)
              hide(state, strangers(model) + labels(model) + (v[:hide_site] ? site_objects(model) : []))
              drop_plane(model, state)
              if v[:section]
                state[:plane] = model.entities.add_section_plane([SU.pt(v[:section][:point]), Geom::Vector3d.new(*v[:section][:normal])])
                model.entities.active_section_plane = state[:plane]
              end
              look(model, style, v[:name].start_with?('axonometria'))
              c = Sketchup::Camera.new(SU.pt(v[:eye]), SU.pt(v[:target]), Geom::Vector3d.new(*v[:up]))
              c.perspective = false
              c.height = v[:height_mm].mm if v[:height_mm]
              view.camera = c
              if v[:fit_building]
                # Framed on the building alone, then the site comes back around it, cropped by the frame.
                around = top_level(model).select { |e| e.visible? && %w[site terrain].include?(SU.kind(e)) }
                around.each { |e| e.visible = false }
                view.zoom_extents
                around.each { |e| e.visible = true }
                view.camera.height = view.camera.height * FIT_BUILDING_MARGIN
              elsif v[:fit]
                view.zoom_extents
              end
              path = File.join(dir, "#{v[:name]}_#{style == 'render' ? 'render' : 'linea'}.png")
              write(view, path, v[:width_px], v[:height_px])
              files << { 'name' => v[:name], 'style' => style, 'path' => path, 'width' => v[:width_px], 'height' => v[:height_px],
                         'px_per_m' => v[:fit] ? nil : px_per_m }
            }]
          end
        end
        units << ['restore view', restore]
        job = UnitJob.new('Plomada: export drawings', units, operation: false) do
          { 'dir' => dir, 'px_per_m' => px_per_m, 'images' => files }
        end
        job.define_singleton_method(:on_abort) { |_e| restore.call }
        job
      end

      # render: textured, soft default shading, shadows on the axonometrics only;
      # line: hidden-line drawing, black edges on white. Section cuts filled dark.
      def look(model, style, shadows)
        ro = model.rendering_options
        set = ->(k, v) { option(ro, k, v) }
        set.('BackgroundColor', Sketchup::Color.new(255, 255, 255))
        set.('DrawGround', false)
        set.('DrawHorizon', false)
        set.('DisplayText', false)
        set.('DisplaySectionPlanes', false)
        set.('DisplaySectionCuts', true)
        set.('SectionCutFilled', true)
        set.('SectionDefaultFillColor', Sketchup::Color.new(30, 30, 28))
        set.('SectionCutWidth', 8)
        set.('EdgeColorMode', 1)
        set.('ForegroundColor', Sketchup::Color.new(0, 0, 0))
        set.('DrawSilhouettes', true)
        set.('SilhouetteWidth', 3)
        if style == 'render'
          set.('RenderMode', 2)
          set.('Texture', true)
          model.shadow_info['DisplayShadows'] = shadows
        else
          set.('RenderMode', 1)
          model.shadow_info['DisplayShadows'] = false
        end
      end

      # A rendering option this SketchUp does not take is skipped, not fatal.
      def option(ro, key, value)
        ro[key] = value
      rescue ArgumentError, TypeError
        nil
      end

      def write(view, path, width, height)
        opts = { filename: path, width: width, height: height, antialias: true, transparent: false }
        ok = begin
          view.write_image(opts.merge(line_scale: 2.0))
        rescue ArgumentError
          view.write_image(opts)
        end
        raise Error.new(Codes::SKETCHUP, "view.write_image returned false for #{path}") unless ok
      end

      def hide(state, ents)
        ents.each do |e|
          next if !e.valid? || e.hidden?

          e.hidden = true
          state[:hidden] << e
        end
      end

      def unhide(state)
        state[:hidden].each { |e| e.hidden = false if e.valid? }
        state[:hidden].clear
      end

      def drop_plane(model, state)
        model.entities.active_section_plane = nil if model.entities.respond_to?(:active_section_plane=)
        state[:plane].erase! if state[:plane]&.valid?
        state[:plane] = nil
      end

      def top_level(model) = model.entities.select { |e| e.is_a?(Sketchup::ComponentInstance) || e.is_a?(Sketchup::Group) }

      # Trees, cars, garden furniture and fences: in front of an elevation they hide the building.
      def site_objects(model)
        top_level(model).select { |e| SU.kind(e) == 'site' && %w[object fence].include?(e.get_attribute(DICT, 'site_kind')) }
      end

      # Anything Plomada did not build, such as the template's scale figure at the origin.
      def strangers(model) = top_level(model).select { |e| SU.kind(e).nil? }

      def labels(model) = model.entities.to_a.select { |e| e.layer.name == 'Ambientes' }
    end
  end
end
