# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative '../geometry'
require_relative 'storeys'
require_relative 'library'

module Plomada
  module SU
    # Walls: one group per storey (N00_muros, tag Muros, MAT_revoque_blanco),
    # filled one wall per step from the solver's faces.
    module Walls
      module_function

      def create_group(model, storey)
        g = SU.group_on(model, model.entities, SU.group_name(storey, 'muros'), 'Muros', 'MAT_revoque_blanco')
        SU.set_attrs(g, 'kind' => 'walls', 'storey' => storey)
      end

      def build_wall(group, faces, record)
        created = SU.add_faces(group.entities, faces, always_build: true)
        json = JSON.generate(record)
        created.each do |face, f|
          next unless face&.valid?

          SU.set_attrs(face, 'kind' => 'wall_face', 'wall' => record['id'], 'part' => f[:part], 'record' => json)
          SU.set_attrs(face, 'segment' => f[:segment], 'curved' => true) if f[:curved]
        end
        created.size
      end

      # The vertical edges between two facets of one curved face (same wall,
      # segment and side) become soft and smooth, so the arc shades as one
      # surface; horizontal edges, jambs and segment joints stay hard.
      def soften_curves(group)
        count = 0
        group.entities.grep(Sketchup::Edge).each do |e|
          faces = e.faces
          next unless faces.size == 2 && faces.all? { |f| f.get_attribute(DICT, 'curved') == true }

          keys = faces.map { |f| %w[wall segment part].map { |k| f.get_attribute(DICT, k) } }
          next unless keys[0] == keys[1]

          v = e.end.position - e.start.position
          next unless v.length.positive? && v.normalize.z.abs > 0.999

          e.soft = true
          e.smooth = true
          count += 1
        end
        count
      end

      # strip_internal_faces: probes 1 mm either side of every face from a
      # point on it; a face with wall on both sides is erased, then any edge
      # left without faces.
      def strip_internal_faces(group, solids, probe = CONFIG[:probe_mm])
        ents = group.entities
        tr = group.transformation
        internal = internal_faces(ents, tr, solids, probe)
        ents.erase_entities(internal) unless internal.empty?
        stray = ents.grep(Sketchup::Edge).select { |e| e.faces.empty? }
        ents.erase_entities(stray) unless stray.empty?
        internal.size
      end

      def internal_faces(ents, tr, solids, probe)
        ents.grep(Sketchup::Face).select do |f|
          point = point_on(f, tr)
          normal = f.normal.transform(tr)
          Geometry.internal_face?(point, normal.to_a, solids, probe)
        end
      end

      # A point inside the face: the centroid of its first mesh triangle.
      def point_on(face, tr)
        mesh = face.mesh(0)
        tri = mesh.polygon_points_at(1)
        c = tri.map { |p| p.transform(tr) }
        SU.to_mm(Geom::Point3d.new((c[0].x + c[1].x + c[2].x) / 3.0, (c[0].y + c[1].y + c[2].y) / 3.0,
                                   (c[0].z + c[1].z + c[2].z) / 3.0))
      end
    end

    # Windows and doors as component instances named after their tag, on tag
    # Carpinterias, with the full opening record in their plomada attributes.
    module Openings
      MATERIALS = { frame: 'MAT_metal', glass: 'MAT_vidrio', wood: 'MAT_madera', metal: 'MAT_porton' }.freeze

      module_function

      def definition_name(frame)
        w = Geometry.fmt(frame[:width])
        h = Geometry.fmt(frame[:height])
        if frame[:kind] == 'window'
          "plomada_ventana_#{w}x#{h}"
        elsif garage?(frame)
          "plomada_porton_#{w}x#{h}_#{Geometry.fmt(frame[:thickness])}_#{frame[:swing]}"
        else
          "plomada_puerta_#{w}x#{h}_#{Geometry.fmt(frame[:thickness])}_#{frame[:swing]}_#{frame[:hand]}"
        end
      end

      def definition(model, frame)
        name = definition_name(frame)
        existing = model.definitions[name]
        return existing if existing && existing.get_attribute(DICT, 'kind') == 'opening_definition' && existing.entities.size.positive?

        defn = existing || model.definitions.add(name)
        defn.entities.clear! if existing
        parts = if frame[:kind] == 'window'
                  Geometry.window_parts(frame[:width], frame[:height])
                elsif garage?(frame)
                  Geometry.garage_door_parts(frame[:width], frame[:height], frame[:thickness], frame[:swing])
                else
                  Geometry.door_parts(frame[:width], frame[:height], frame[:thickness], frame[:swing], frame[:hand])
                end
        parts.each { |part| add_part(model, defn.entities, part) }
        SU.set_attrs(defn, 'kind' => 'opening_definition', 'opening_kind' => frame[:kind])
        defn
      end

      # A door at least garage_door_min_width_mm wide is a sectional garage door.
      def garage?(frame) = frame[:kind] == 'door' && frame[:width] >= CONFIG[:garage_door_min_width_mm]

      def add_part(model, entities, part)
        g = entities.add_group
        g.name = part[:name]
        pivot = part[:pivot] || [0.0, 0.0, 0.0]
        min = [0, 1, 2].map { |k| part[:min][k] - pivot[k] }
        max = [0, 1, 2].map { |k| part[:max][k] - pivot[k] }
        SU.add_faces(g.entities, SU.faces_from_loops(Geometry.box_faces(min, max)))
        g.transformation = Geom::Transformation.translation(SU.pt(pivot)) if part[:pivot]
        g.material = SU.material(model, MATERIALS.fetch(part[:material]))
        attrs = { 'part' => part[:name] }
        attrs.merge!('hinge' => part[:hinge], 'open_angle' => 0.0) if part[:hinge]
        SU.set_attrs(g, attrs)
      end

      def transformation(frame)
        Geom::Transformation.axes(SU.pt(frame[:origin]), SU.vec(frame[:xaxis]), SU.vec(frame[:yaxis]), Z_AXIS)
      end

      def place(model, frame)
        inst = model.entities.add_instance(definition(model, frame), transformation(frame))
        inst.name = frame[:tag].to_s
        inst.layer = SU.tag(model, 'Carpinterias')
        SU.set_attrs(inst, 'kind' => 'opening', 'id' => frame[:id], 'wall' => frame[:wall],
                           'opening_kind' => frame[:kind], 'record' => JSON.generate(frame[:record]))
        SU.set_attrs(inst, 'panel' => frame[:panel], 'panels' => frame[:panels]) if frame[:panels]
        inst
      end

      # +elevation+ (mm): the storey the opening is on; frames are storey-local.
      def move(model, inst, frame, elevation: 0.0)
        defn = definition(model, frame)
        inst.definition = defn unless inst.definition == defn
        lift = Geom::Transformation.translation(Geom::Vector3d.new(0, 0, elevation.to_f.mm))
        inst.transformation = lift * transformation(frame)
        SU.set_attrs(inst, 'record' => JSON.generate(frame[:record]), 'wall' => frame[:wall])
      end
    end

    # Floor slab (N00_losa) and roof (N00_techo), both on tag Losas.
    module Slabs
      module_function

      # +holes+: stair wells (plan rings, mm); one that is not wholly inside the
      # outline belongs to another building and is left out.
      def slab(model, storey, outline, thickness, name: SU.group_name(storey, 'losa'), holes: [])
        g = SU.group_on(model, model.entities, name, 'Losas', 'MAT_piso_porcelanato')
        res = Geometry.slab_faces(outline, holes, -thickness, 0.0)
        SU.add_faces(g.entities, res[:faces], always_build: true)
        SU.soften(g.entities)
        SU.set_attrs(g, 'kind' => 'slab', 'storey' => storey, 'thickness' => thickness,
                        'outline' => JSON.generate(outline), 'wells' => holes.size - res[:skipped].size)
      end

      # Slab records (0.5): one group per top elevation (N00_losa, N00_losa_2...)
      # holding the union of the slabs at that top, each group carrying the
      # records it was built from, and one N00_canto_<id> group per edge band.
      # Returns the names of the groups it made.
      def records(model, storey, layout, records, warnings = [])
        by_id = records.to_h { |r| [r['id'], r] }
        names = []
        layout[:slabs].each_with_index do |s, i|
          name = SU.group_name(storey, 'losa') + (i.zero? ? '' : "_#{i + 1}")
          g = SU.group_on(model, model.entities, name, 'Losas', slab_material(model, s[:material], warnings))
          SU.add_faces(g.entities, s[:faces], always_build: true)
          SU.soften(g.entities)
          recs = s[:ids].map { |id| { 'v' => 1, 'kind' => 'slab' }.merge(by_id[id]) }
          SU.set_attrs(g, 'kind' => 'slab', 'storey' => storey, 'thickness' => s[:thickness], 'top' => s[:top],
                          'ids' => JSON.generate(s[:ids]), 'records' => JSON.generate(recs))
          names << name
        end
        layout[:bands].each do |b|
          name = SU.group_name(storey, "canto_#{b[:id]}")
          g = SU.group_on(model, model.entities, name, 'Losas', 'MAT_revoque_blanco')
          SU.add_faces(g.entities, b[:faces], always_build: true)
          SU.soften(g.entities)
          SU.set_attrs(g, 'kind' => 'slab_band', 'storey' => storey, 'slab' => b[:id])
          names << name
        end
        names
      end

      def slab_material(model, name, warnings)
        return name if CONFIG[:materials].key?(name) || model.materials[name]

        warnings << "slab material #{name.inspect} is not in the model; MAT_piso_porcelanato used"
        'MAT_piso_porcelanato'
      end

      # The slab records stored on a storey's slab groups.
      def stored_records(model, storey)
        Storeys.entities(model, storey).select { |g| SU.kind(g) == 'slab' }.flat_map do |g|
          raw = g.get_attribute(DICT, 'records')
          raw ? JSON.parse(raw) : []
        end
      end

      def flat_roof(model, storey, outline, wall_top, thickness, overhang, name: SU.group_name(storey, 'techo'))
        g = SU.group_on(model, model.entities, name, 'Losas', 'MAT_hormigon')
        roof = Geometry.flat_roof_outline(outline, overhang)
        SU.add_faces(g.entities, SU.faces_from_loops(Geometry.prism_faces(roof, wall_top, wall_top + thickness)))
        SU.soften(g.entities)
        SU.set_attrs(g, 'kind' => 'roof', 'roof' => 'flat', 'storey' => storey, 'thickness' => thickness,
                        'overhang' => overhang)
      end

      # A gable (with its end walls) or a hip roof: +roof+ is what
      # Geometry.gable_roof or Geometry.hip_roof returned.
      def pitched_roof(model, storey, roof, kind, thickness, overhang, pitch, name: SU.group_name(storey, 'techo'))
        g = SU.group_on(model, model.entities, name, 'Losas', 'MAT_hormigon')
        sheet = SU.group_on(model, g.entities, 'cubierta', nil, nil)
        SU.add_faces(sheet.entities, SU.faces_from_loops(roof[:roof]))
        roof[:gables].each_with_index do |faces, i|
          end_wall = SU.group_on(model, g.entities, "hastial_#{i + 1}", nil, 'MAT_revoque_blanco')
          SU.add_faces(end_wall.entities, SU.faces_from_loops(faces))
        end
        SU.set_attrs(g, 'kind' => 'roof', 'roof' => kind, 'storey' => storey, 'thickness' => thickness,
                        'overhang' => overhang, 'pitch' => pitch)
      end
    end

    # Furniture: one component definition per catalogue item (Plomada_<item>),
    # one instance per placed block, on tag Mobiliario.
    module Furniture
      MATERIAL = { 'furniture' => 'MAT_mobiliario', 'sanitary' => 'MAT_sanitario' }.freeze

      module_function

      def definition(model, item)
        name = "Plomada_#{item}"
        defn = model.definitions[name]
        return defn if defn && SU.kind(defn) == 'furniture_definition'

        res = Geometry.furniture_parts(item)
        defn = model.definitions.add(name)
        mat = SU.material(model, MATERIAL[res[:family]])
        res[:parts].each_with_index do |faces, i|
          g = defn.entities.add_group
          g.name = "pieza_#{i + 1}"
          g.material = mat
          SU.add_faces(g.entities, SU.faces_from_loops(faces))
        end
        SU.set_attrs(defn, 'kind' => 'furniture_definition', 'item' => item, 'family' => res[:family])
        defn
      end

      # The library component for the item when there is one (scaled to the
      # catalogue footprint), else the massing.
      def place(model, rec, library: nil)
        entry = library && Library.entry(library, 'catalogo_acadmcp', rec['item'])
        if entry
          _, w, d, = Geometry::FURNITURE.fetch(rec['item'])
          inst = Library.place(model, model.entities, entry, rec['at'], rec['rotation'], 0.0, w: w.to_f, d: d.to_f)
          inst.name = rec['item']
          inst.layer = SU.tag(model, 'Mobiliario')
          return SU.set_attrs(inst, 'kind' => 'furniture', 'id' => rec['id'], 'item' => rec['item'], 'source' => 'library',
                                    'record' => JSON.generate({ 'v' => 1, 'kind' => 'furniture' }.merge(rec)))
        end

        tr = Geom::Transformation.translation(SU.pt(rec['at'] + [0.0])) *
             Geom::Transformation.rotation(ORIGIN, Z_AXIS, rec['rotation'].to_f.degrees)
        inst = model.entities.add_instance(definition(model, rec['item']), tr)
        inst.name = rec['item']
        inst.layer = SU.tag(model, 'Mobiliario')
        SU.set_attrs(inst, 'kind' => 'furniture', 'id' => rec['id'], 'item' => rec['item'],
                           'record' => JSON.generate({ 'v' => 1, 'kind' => 'furniture' }.merge(rec)))
      end
    end

    # Terrain: a lawn slab (Terreno, tag Entorno, MAT_cesped) whose top is the
    # ground line, with a hole under every building so nothing overlaps.
    module Terrain
      module_function

      def build(model, outer, holes, top, thickness, material)
        g = SU.group_on(model, model.entities, 'Terreno', 'Entorno', material)
        res = Geometry.slab_faces(outer, holes, top - thickness, top)
        SU.add_faces(g.entities, res[:faces], always_build: true)
        SU.set_attrs(g, 'kind' => 'terrain', 'storey' => 'terrain', 'top' => top, 'holes' => holes.size)
      end

      # The terrain as a region (Geometry::Boolean): overlapping holes (a
      # basement under the ground floor, a ramp cut) merge instead of fighting.
      def build_region(model, region, top, thickness, material, hole_count)
        g = SU.group_on(model, model.entities, 'Terreno', 'Entorno', material)
        faces = region.flat_map { |r| Geometry.region_prism(r[:outer], r[:holes], top - thickness, top) }
        SU.add_faces(g.entities, faces, always_build: true)
        SU.soften(g.entities)
        SU.set_attrs(g, 'kind' => 'terrain', 'storey' => 'terrain', 'top' => top, 'holes' => hole_count)
      end
    end

    # Room labels: 3D text on tag Ambientes, centred on the room point at z 10 mm.
    module Rooms
      module_function

      def label(model, room)
        info = Geometry.room_label(room)
        g = SU.group_on(model, model.entities, "Ambiente #{room['id']} #{room['name']}", 'Ambientes', 'MAT_metal')
        g.entities.add_3d_text(info[:text], TextAlignCenter, 'Arial', true, false,
                               CONFIG[:room_label_height_mm].mm, 0.0, 0.0, true, 0.0)
        bb = g.entities.parent.bounds
        center = bb.center
        target = SU.pt(info[:at])
        g.transformation = Geom::Transformation.translation(Geom::Vector3d.new(target.x - center.x, target.y - center.y,
                                                                               target.z - bb.min.z))
        SU.set_attrs(g, 'kind' => 'room', 'id' => room['id'], 'record' => JSON.generate(room))
      end
    end

    # Reads the plan of one storey back from the plomada attributes in the
    # model; with no storey named, the lowest one.
    module PlanReader
      module_function

      def read(model, storey = nil)
        storey ||= Storeys.resolve(model, nil, required: false)
        walls = {}
        model.entities.grep(Sketchup::Group).each do |g|
          next unless g.valid? && SU.kind(g) == 'walls' && Storeys.of(g) == storey

          g.entities.grep(Sketchup::Face).each do |f|
            id = f.get_attribute(DICT, 'wall')
            next if id.nil? || walls.key?(id)

            walls[id] = SU.record(f)
          end
        end
        mine = Storeys.entities(model, storey)
        of_kind = ->(cls, kind) { mine.select { |e| e.is_a?(cls) && SU.kind(e) == kind } }
        {
          'walls' => walls.values.compact,
          # An opening on an arc is one component per panel, all carrying the same record.
          'openings' => of_kind.call(Sketchup::ComponentInstance, 'opening').map { |i| SU.record(i) }.compact
                               .uniq { |r| r['id'] },
          'rooms' => of_kind.call(Sketchup::Group, 'room').map { |g| SU.record(g) }.compact,
          'stairs' => of_kind.call(Sketchup::Group, 'stair').map { |g| SU.record(g) }.compact,
          'furniture' => of_kind.call(Sketchup::ComponentInstance, 'furniture').map { |i| SU.record(i) }.compact,
          'slabs' => Slabs.stored_records(model, storey),
          **Plan::EXTRA_KINDS.reject { |key, _| key == 'slabs' }.to_h do |key, kind|
            [key, of_kind.call(Sketchup::Group, kind).map { |g| SU.record(g) }.compact]
          end,
          'storey' => storey_settings(model, storey),
          'storeys' => Storeys.list(model)
        }
      end

      def storey_settings(model, storey = nil)
        storey ||= Storeys.resolve(model, nil, required: false)
        g = model.entities.grep(Sketchup::Group).find { |x| x.valid? && SU.kind(x) == 'walls' && Storeys.of(x) == storey }
        raw = g&.get_attribute(DICT, 'storey_settings')
        raw ? JSON.parse(raw) : { 'name' => storey, 'height' => CONFIG[:storey_height_mm], 'elevation' => 0.0 }
      end
    end
  end
end
