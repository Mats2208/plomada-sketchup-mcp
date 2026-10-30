# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative 'library'
require_relative '../geometry'

module Plomada
  module SU
    # The site, on tag Entorno: library components for the SITIO_ blocks
    # (trees, cars, garden furniture, people), paving and decks, fences and
    # pools. Everything stands on the ground (the terrain top).
    module Site
      # SITIO_<NAME> block -> key of the "sitio" section of mapa_componentes.json.
      OBJECTS = {
        'AUTO' => 'car_sedan', 'SUV' => 'car_suv', 'PICKUP' => 'car_pickup',
        'ARBOL' => 'tree', 'PALMERA' => 'palm', 'PINO' => 'pine', 'ARBUSTO' => 'hedge', 'SETO' => 'hedge',
        'MACETA' => 'potted_plant', 'REPOSERA' => 'sun_lounger', 'PARRILLA' => 'bbq',
        'MESA_JARDIN' => 'outdoor_dining', 'PERGOLA' => 'pergola', 'PERSONA' => 'person', 'FAROLA' => 'street_light'
      }.freeze
      TREES = %w[ARBOL PALMERA PINO].freeze
      # Library keys whose stand-in is an ellipsoid; any other heavy model stands in as a box.
      VEGETATION = %w[tree palm pine hedge potted_plant].freeze
      # Library keys an AI pass should draw itself (export_scene_images soft_objects): cars and plants.
      SOFT = (VEGETATION + %w[car_sedan car_suv car_pickup]).freeze

      module_function

      def key_for(item) = OBJECTS.fetch(item.to_s.upcase, item.to_s.downcase)

      # Returns the instance, or nil when the library has no component for it. With +proxy+ the instance keeps
      # its place and size but shows a massing stand-in (Site.proxy_definition) instead of the library model.
      def object(model, rec, library, ground, proxy: false)
        entry = library && Library.entry(library, 'sitio', key_for(rec['item']))
        return nil unless entry

        inst = Library.place(model, model.entities, entry, rec['at'], rec['rotation'], ground)
        inst.definition = proxy_definition(model, inst.definition, VEGETATION.include?(key_for(rec['item']))) if proxy
        inst.name = "sitio_#{rec['item'].to_s.downcase}"
        inst.layer = SU.tag(model, 'Entorno')
        SU.set_attrs(inst, 'kind' => 'site', 'site_kind' => 'object', 'id' => rec['id'], 'item' => rec['item'],
                           'record' => JSON.generate(rec))
      end

      # The massing stand-in for a library definition, filling the same bounding box (so an instance switched to
      # it keeps its place and size): an ellipsoid for vegetation, a box for the rest. One per library model.
      def proxy_definition(model, defn, round)
        key = "proxy:#{defn.get_attribute(DICT, 'library_path') || defn.name}"
        found = model.definitions.find { |d| d.get_attribute(DICT, 'proxy_of') == key }
        return found if found

        bb = defn.bounds
        stand_in = model.definitions.add("plomada_proxy_#{defn.name}")
        SU.add_faces(stand_in.entities, SU.faces_from_loops(Geometry.proxy_faces(SU.to_mm(bb.min), SU.to_mm(bb.max), round)),
                     always_build: true)
        SU.soften(stand_in.entities, 40.0) if round
        material = SU.material(model, round ? 'MAT_vegetacion' : 'MAT_hormigon')
        stand_in.entities.grep(Sketchup::Face).each { |f| f.material = material }
        stand_in.set_attribute(DICT, 'proxy_of', key)
        stand_in
      end

      # Faces of one library model per site key ({key => faces}), loading what the library has.
      def library_faces(model, library, keys)
        keys.to_h do |k|
          entry = library && Library.entry(library, 'sitio', k)
          [k, entry ? Library.face_count(Library.definition_for(model, entry)) : 0]
        end
      end

      def paving(model, rec, ground, deck: false)
        name = deck ? "deck_#{rec['id']}" : "pavimento_#{rec['id']}"
        g = SU.group_on(model, model.entities, name, 'Entorno', deck ? 'MAT_madera' : 'MAT_pavimento')
        SU.add_faces(g.entities, SU.faces_from_loops(Geometry.paving_faces(ring(rec), ground)))
        SU.soften(g.entities)
        SU.set_attrs(g, 'kind' => 'site', 'site_kind' => deck ? 'deck' : 'paving', 'id' => rec['id'],
                        'record' => JSON.generate(rec))
      end

      def fence(model, rec, ground)
        g = SU.group_on(model, model.entities, "cerco_#{rec['id']}", 'Entorno', 'MAT_madera')
        pts = Geometry::Arc.polyline(rec['points'], closed: rec['closed'])
        Geometry.fence_parts(pts, rec['closed'], ground, rec['height']).each do |part|
          sub = SU.group_on(model, g.entities, part[:name], nil, nil)
          SU.add_faces(sub.entities, SU.faces_from_loops(part[:faces]))
        end
        SU.set_attrs(g, 'kind' => 'site', 'site_kind' => 'fence', 'id' => rec['id'], 'record' => JSON.generate(rec))
      end

      def pool(model, rec, ground)
        parts = Geometry.pool_parts(ring(rec), ground, rec['depth'])
        g = SU.group_on(model, model.entities, "pileta_#{rec['id']}", 'Entorno', nil)
        { 'borde' => [parts[:coping], 'MAT_pavimento', true], 'vaso' => [parts[:walls], 'MAT_revoque_blanco', true],
          'fondo' => [parts[:floor], 'MAT_revoque_blanco', false], 'agua' => [parts[:water], 'MAT_agua', false] }
          .each do |name, (faces, mat, holed)|
            sub = SU.group_on(model, g.entities, name, nil, mat)
            SU.add_faces(sub.entities, holed ? faces : SU.faces_from_loops(faces), always_build: holed)
          end
        SU.set_attrs(g, 'kind' => 'site', 'site_kind' => 'pool', 'id' => rec['id'], 'record' => JSON.generate(rec),
                        'outline' => JSON.generate(parts[:outer]))
      end

      # A site polygon with its arcs tessellated.
      def ring(rec) = Geometry::Arc.polyline(rec['points'], closed: true)

      # Outer rings of the pools in the model, for the terrain's holes (mm, world plan).
      def pool_outlines(model)
        model.entities.grep(Sketchup::Group).select do |g|
          g.valid? && ((SU.kind(g) == 'site' && g.get_attribute(DICT, 'site_kind') == 'pool') ||
                       (SU.kind(g) == 'pool' && g.get_attribute(DICT, 'raised') == false))
        end
             .map do |g|
               off = SU.to_mm(g.transformation.origin)
               JSON.parse(g.get_attribute(DICT, 'outline')).map { |p| [p[0] + off[0], p[1] + off[1]] }
             end
      end
    end
  end
end
