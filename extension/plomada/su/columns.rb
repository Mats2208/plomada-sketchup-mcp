# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative '../geometry'

module Plomada
  module SU
    # Columns (tag Estructura): one closed group per column
    # (N00_columna_<id>), from the floor up to its height or the storey's.
    module Columns
      module_function

      def build(model, storey, rec, storey_height)
        g = SU.group_on(model, model.entities, SU.group_name(storey, "columna_#{rec['id']}"), 'Estructura', rec['material'])
        SU.add_faces(g.entities, SU.faces_from_loops(Geometry.column_faces(rec, 0.0, rec['height'] || storey_height)),
                     always_build: true)
        SU.soften(g.entities)
        SU.set_attrs(g, 'kind' => 'column', 'id' => rec['id'], 'storey' => storey,
                        'record' => JSON.generate({ 'v' => 1, 'kind' => 'column' }.merge(rec)))
      end
    end
  end
end
