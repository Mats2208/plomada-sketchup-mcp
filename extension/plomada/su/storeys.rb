# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative '../geometry'

module Plomada
  module SU
    # Storeys (N00, N01, ...). Every storey is built at z 0 like a one-storey
    # house, then everything the job created is stamped with the storey name
    # and lifted to the storey's elevation in one transform. Objects without a
    # storey stamp (houses built before 0.2) belong to the ground storey.
    #
    # Elevation of a new storey: the top of the highest storey below it
    # (elevation + wall height) plus this storey's slab thickness, so its slab
    # sits on the walls underneath and its floor is at the elevation.
    module Storeys
      module_function

      def default_name = CONFIG[:storey_prefix]

      def of(entity) = entity.get_attribute(DICT, 'storey') || default_name

      def entities(model, storey)
        SU.plomada_entities(model).select { |e| of(e) == storey }
      end

      # [{'name', 'elevation', 'height', 'slab_thickness'}] sorted by elevation.
      def list(model)
        model.entities.grep(Sketchup::Group).select { |g| g.valid? && SU.kind(g) == 'walls' }.map do |g|
          s = settings_of(g)
          { 'name' => of(g), 'elevation' => s.fetch('elevation', 0.0).to_f, 'height' => s.fetch('height', CONFIG[:storey_height_mm]).to_f,
            'slab_thickness' => s.fetch('slab_thickness', CONFIG[:slab_thickness_mm]).to_f }
        end.sort_by { |s| s['elevation'] }
      end

      def settings_of(group)
        raw = group.get_attribute(DICT, 'storey_settings')
        raw ? JSON.parse(raw) : {}
      end

      # Elevation (mm) for +name+: explicit wins; an existing storey keeps its
      # own; a new one goes on top of the highest other storey.
      def elevation_for(model, name, explicit, slab_thickness)
        Geometry::Levels.elevation_for(list(model), name, explicit, slab_thickness)
      end

      # The storey right below +elevation+ by elevation (the one whose stairs land on it).
      def below(model, elevation, name) = Geometry::Levels.below(list(model), elevation, name)

      # The ground level (mm) the site and the terrain stand on: the last one a
      # build stored in the model, else ground_level_mm.
      def ground_level(model)
        stored = model.get_attribute(DICT, 'ground_level_mm')
        stored.nil? ? CONFIG[:ground_level_mm] : stored.to_f
      end

      def ground_level_stored?(model) = !model.get_attribute(DICT, 'ground_level_mm').nil?

      def store_ground_level(model, value) = model.set_attribute(DICT, 'ground_level_mm', value.to_f)

      # The storey an edit or read acts on: the one named, else the only one;
      # with several storeys a name is required (wall ids repeat per floor).
      def resolve(model, value, required: true)
        names = list(model).map { |s| s['name'] }
        if value.nil?
          return names.first || default_name if names.size <= 1 || !required

          raise InvalidParams, "this model has storeys #{names.join(', ')}; pass storey to say which one"
        end

        name = Build.storey_name(value, 'storey')
        return name if names.include?(name) || names.empty?

        raise InvalidParams, "storey #{name.inspect} is not in the model; it has #{names.join(', ')}"
      end

      def snapshot(model) = SU.plomada_entities(model).map(&:entityID).to_h { |id| [id, true] }

      # Stamps what appeared since +before+ with the storey and lifts it.
      def stamp_and_lift(model, before, name, elevation)
        fresh = SU.plomada_entities(model).reject { |e| before[e.entityID] }
        fresh.each { |e| e.set_attribute(DICT, 'storey', name) }
        lift(model, fresh, elevation)
        fresh.size
      end

      def lift(model, ents, elevation)
        return if ents.empty? || elevation.abs < 1e-9

        model.entities.transform_entities(Geom::Transformation.translation(Geom::Vector3d.new(0, 0, elevation.mm)), ents)
      end

      # Stair wells for a storey at +elevation+: the footprints of the stairs on
      # the storey below, in world plan coordinates.
      def wells(model, elevation, name)
        under = below(model, elevation, name)
        return [] unless under

        model.entities.grep(Sketchup::Group).select { |g| g.valid? && SU.kind(g) == 'stair' && of(g) == under['name'] }
             .map { |g| JSON.parse(g.get_attribute(DICT, 'footprint')) }
      end
    end

    # Stairs: one group per stair (N00_escalera_S1) on tag Escaleras, one
    # closed sub-group per flight and landing, in MAT_hormigon.
    module Stairs
      module_function

      def build(model, storey, stair, layout)
        g = SU.group_on(model, model.entities, SU.group_name(storey, "escalera_#{stair['id']}"), 'Escaleras', 'MAT_hormigon')
        layout[:pieces].each do |piece|
          sub = SU.group_on(model, g.entities, piece[:name], nil, nil)
          SU.add_faces(sub.entities, SU.faces_from_loops(piece[:faces]))
        end
        SU.set_attrs(g, 'kind' => 'stair', 'id' => stair['id'], 'record' => JSON.generate(record(stair)),
                        'footprint' => JSON.generate(layout[:footprint]), 'top' => layout[:top])
      end

      # The AutoCAD MCP Pro payload: the stair's own kind travels as stair_kind.
      def record(stair)
        { 'v' => 1, 'kind' => 'stair', 'stair_kind' => stair['kind'] }.merge(stair.reject { |k, _| k == 'kind' })
      end
    end
  end
end
