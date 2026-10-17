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

      module_function

      def key_for(item) = OBJECTS.fetch(item.to_s.upcase, item.to_s.downcase)

      # Returns the instance, or nil when the library has no component for it.
      def object(model, rec, library, ground)
        entry = library && Library.entry(library, 'sitio', key_for(rec['item']))
        return nil unless entry

        inst = Library.place(model, model.entities, entry, rec['at'], rec['rotation'], ground)
        inst.name = "sitio_#{rec['item'].to_s.downcase}"
        inst.layer = SU.tag(model, 'Entorno')
        SU.set_attrs(inst, 'kind' => 'site', 'site_kind' => 'object', 'id' => rec['id'], 'item' => rec['item'],
                           'record' => JSON.generate(rec))
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
