# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative 'library'
require_relative 'site'
require_relative 'railings'
require_relative '../geometry'

module Plomada
  module SU
    # Planters (tag Entorno): N00_jardinera_<id> with the curb, the soil and
    # the plants, library components from the sitio section or massing spheres.
    module Planters
      MASSING = 'Plomada_planta'

      module_function

      def build(model, storey, rec, footing, library, warnings)
        base, = Footing.base(footing, rec['on'])
        lay = Geometry.planter_layout(rec, base)
        g = SU.group_on(model, model.entities, SU.group_name(storey, "jardinera_#{rec['id']}"), 'Entorno', nil)
        curb = SU.group_on(model, g.entities, 'borde', nil, 'MAT_hormigon')
        SU.add_faces(curb.entities, lay[:curb], always_build: true)
        SU.soften(curb.entities)
        soil = SU.group_on(model, g.entities, 'tierra', nil, 'MAT_tierra')
        SU.add_faces(soil.entities, SU.faces_from_loops(lay[:soil]), always_build: true)
        SU.soften(soil.entities)
        missing = []
        heavy = {}
        lay[:plants].each do |pl|
          entry = library && Library.entry(library, 'sitio', Site.key_for(pl[:key]))
          if entry && light?(model, entry, pl[:key], heavy)
            inst = Library.place(model, g.entities, entry, pl[:at], pl[:rotation], pl[:z])
            inst.name = "planta_#{pl[:key]}"
          else
            missing << pl[:key] unless heavy.key?(pl[:key])
            g.entities.add_instance(massing(model), Geom::Transformation.translation(SU.pt(pl[:at] + [pl[:z]])))
          end
        end
        unless missing.empty?
          warnings << "planter #{rec['id']}: no library component for #{missing.uniq.join(', ')}; massing spheres used"
        end
        heavy.each do |key, faces|
          warnings << "planter #{rec['id']}: the library #{key} has #{faces} faces (over plant_max_faces " \
                      "#{CONFIG[:plant_max_faces]}); massing spheres used"
        end
        SU.set_attrs(g, 'kind' => 'planter', 'id' => rec['id'], 'storey' => storey, 'plants' => lay[:plants].size,
                        'record' => JSON.generate({ 'v' => 1, 'kind' => 'planter' }.merge(rec)))
      end

      # A library plant light enough to repeat across a planter; a heavier one
      # is noted in +heavy+ and stands in as a massing sphere.
      def light?(model, entry, key, heavy)
        return false if heavy.key?(key)

        faces = Library.face_count(Library.definition(model, entry[:path], entry[:part]))
        return true if faces <= CONFIG[:plant_max_faces]

        heavy[key] = faces
        false
      end

      # One shared definition for every massing plant: a sphere of
      # plant_massing_mm standing on its origin.
      def massing(model)
        defn = model.definitions[MASSING]
        return defn if defn && SU.kind(defn) == 'plant_definition'

        defn = model.definitions.add(MASSING)
        r = CONFIG[:plant_massing_mm] / 2.0
        SU.add_faces(defn.entities, SU.faces_from_loops(Geometry.sphere_faces([0.0, 0.0], r, 0.0)), always_build: true)
        SU.soften(defn.entities, 40.0)
        defn.entities.grep(Sketchup::Face).each { |f| f.material = SU.material(model, 'MAT_vegetacion') }
        SU.set_attrs(defn, 'kind' => 'plant_definition')
        defn
      end
    end
  end
end
