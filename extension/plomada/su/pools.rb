# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative 'railings'
require_relative '../geometry'

module Plomada
  module SU
    # Pool records on any storey (tag Entorno, N00_pileta_<id>): recessed on
    # the storey that holds the site, a raised basin on any other (raised
    # auto), or as the record says.
    module Pools
      module_function

      def build(model, storey, rec, footing, ground_storey)
        base, = Footing.base(footing, rec['on'])
        raised = rec['raised'] == 'auto' ? !ground_storey : rec['raised']
        lay = Geometry.pool_layout(rec, base, raised)
        g = SU.group_on(model, model.entities, SU.group_name(storey, "pileta_#{rec['id']}"), 'Entorno', nil)
        lay[:parts].each do |name, (faces, mat, holed)|
          sub = SU.group_on(model, g.entities, name, nil, mat)
          SU.add_faces(sub.entities, holed ? faces : SU.faces_from_loops(faces), always_build: true)
          SU.soften(sub.entities)
        end
        SU.set_attrs(g, 'kind' => 'pool', 'id' => rec['id'], 'storey' => storey, 'raised' => lay[:raised],
                        'outline' => JSON.generate(lay[:outer]),
                        'record' => JSON.generate({ 'v' => 1, 'kind' => 'pool' }.merge(rec)))
      end
    end
  end
end
