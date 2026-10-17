# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative 'library'
require_relative 'site'
require_relative '../geometry'

module Plomada
  module SU
    # What a storey's 0.5 parts stand on: the plan's slab records, by id, with
    # their top (storey-local) and their tessellated outlines.
    module Footing
      module_function

      def of(plan)
        plan['slabs'].to_h do |s|
          [s['id'], { top: s['top_offset'], ring: Geometry.slab_shape(s)[:outer] }]
        end
      end

      # The base z and the slab rings a part with +on+ (a slab id or nil) uses.
      def base(footing, on)
        return [footing[on][:top], [footing[on][:ring]]] if on

        [0.0, footing.values.map { |f| f[:ring] }]
      end
    end

    # Railings (tag Barandas): one group per railing (N00_baranda_<id>), one
    # closed sub-group per glass panel, post, baluster, rail or parapet.
    module Railings
      module_function

      def build(model, storey, rec, footing)
        base, rings = Footing.base(footing, rec['on'])
        res = Geometry.railing_parts(rec, rings, base)
        g = SU.group_on(model, model.entities, SU.group_name(storey, "baranda_#{rec['id']}"), 'Barandas', nil)
        res[:parts].each do |part|
          sub = SU.group_on(model, g.entities, part[:name], nil, part[:material])
          SU.add_faces(sub.entities, SU.faces_from_loops(part[:faces]), always_build: true)
          SU.soften(sub.entities)
        end
        SU.set_attrs(g, 'kind' => 'railing', 'id' => rec['id'], 'storey' => storey,
                        'record' => JSON.generate({ 'v' => 1, 'kind' => 'railing' }.merge(rec)))
      end
    end
  end
end
