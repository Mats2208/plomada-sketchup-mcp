# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative 'storeys'
require_relative '../geometry'

module Plomada
  module SU
    # Ramps (tag Entorno, N00_rampa_<id>): the sloped slab and its side walls,
    # one closed sub-group each. The plan footprint is kept for the terrain,
    # which leaves a hole where a ramp runs down into a basement.
    module Ramps
      module_function

      def build(model, storey, rec)
        lay = Geometry.ramp_layout(rec)
        g = SU.group_on(model, model.entities, SU.group_name(storey, "rampa_#{rec['id']}"), 'Entorno', nil)
        lay[:parts].each do |name, faces|
          sub = SU.group_on(model, g.entities, name, nil, name == 'rampa' ? 'MAT_hormigon' : 'MAT_revoque_blanco')
          SU.add_faces(sub.entities, SU.faces_from_loops(faces), always_build: true)
          SU.soften(sub.entities)
        end
        SU.set_attrs(g, 'kind' => 'ramp', 'id' => rec['id'], 'storey' => storey, 'slope' => lay[:slope],
                        'outline' => JSON.generate(lay[:outline]),
                        'record' => JSON.generate({ 'v' => 1, 'kind' => 'ramp' }.merge(rec)))
      end

      # Plan footprints (mm) of the ramps on the named storeys.
      def outlines(model, storeys)
        model.entities.grep(Sketchup::Group).select { |g| g.valid? && SU.kind(g) == 'ramp' && storeys.include?(Storeys.of(g)) }
             .map { |g| JSON.parse(g.get_attribute(DICT, 'outline')) }
      end
    end
  end
end
