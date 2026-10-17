# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative '../geometry'

module Plomada
  module SU
    # Curtain walls (tag Carpinterias, N00_muro_cortina_<id>): one closed
    # sub-group per mullion, rail, transom and pane. Where the path runs in a
    # wall, the wall solver has already left the void (Geometry.curtain_voids).
    module CurtainWalls
      module_function

      def build(model, storey, rec, storey_height)
        res = Geometry.curtain_wall_parts(rec, rec['head'] || storey_height)
        g = SU.group_on(model, model.entities, SU.group_name(storey, "muro_cortina_#{rec['id']}"), 'Carpinterias', nil)
        res[:parts].each do |part|
          sub = SU.group_on(model, g.entities, part[:name], nil, part[:material])
          SU.add_faces(sub.entities, SU.faces_from_loops(part[:faces]), always_build: true)
        end
        SU.set_attrs(g, 'kind' => 'curtain_wall', 'id' => rec['id'], 'storey' => storey, 'mullions' => res[:mullions],
                        'panes' => res[:panes], 'record' => JSON.generate({ 'v' => 1, 'kind' => 'curtain_wall' }.merge(rec)))
      end
    end
  end
end
