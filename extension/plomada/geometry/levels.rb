# frozen_string_literal: true

require_relative 'core'
require_relative '../config'

module Plomada
  module Geometry
    # Where storeys go and which one stands on the ground, as pure functions
    # over the storey list SU::Storeys reads from the model:
    # [{'name', 'elevation', 'height', 'slab_thickness'}] (mm). Elevations are
    # floor levels; 'height' is the wall height.
    #
    # A storey built with an explicit elevation (elevation_mode explicit) may
    # sit anywhere from elevation_min_mm to elevation_max_mm, half levels and
    # basements included; an auto storey goes on top of the highest other
    # one, as in 0.4.
    module Levels
      module_function

      def sorted(storeys) = storeys.sort_by { |s| s['elevation'] }

      # Elevation for +name+: explicit wins; an existing storey keeps its own;
      # a new one goes on top of the highest other storey (its walls plus this
      # storey's slab).
      def elevation_for(storeys, name, explicit, slab_thickness)
        return explicit.to_f unless explicit.nil?

        same = storeys.find { |s| s['name'] == name }
        return same['elevation'] if same

        others = storeys.reject { |s| s['name'] == name }
        return 0.0 if others.empty?

        others.map { |s| s['elevation'] + s['height'] }.max + slab_thickness
      end

      # The storey right below +elevation+ by elevation (whatever order they
      # were built in): its stairs cut the wells of the storey at +elevation+.
      def below(storeys, elevation, name)
        storeys.reject { |s| s['name'] == name }.select { |s| s['elevation'] < elevation - 1.0 }.max_by { |s| s['elevation'] }
      end

      def above(storeys, elevation, name)
        storeys.reject { |s| s['name'] == name }.select { |s| s['elevation'] > elevation + 1.0 }.min_by { |s| s['elevation'] }
      end

      # The storey the site belongs to: the one whose floor is closest to the
      # ground level, if it is within site_snap_mm of it; nil otherwise.
      def site_storey(storeys, ground_level, config = CONFIG)
        best = storeys.min_by { |s| [(s['elevation'] - ground_level).abs, s['elevation']] }
        return nil unless best && (best['elevation'] - ground_level).abs <= config[:site_snap_mm] + 1e-9

        best['name']
      end

      # Storey-local z of the ground the site stands on: the ground level
      # itself when the plan says site_on_ground, else the underside of the
      # storey's slab (the 0.4 rule).
      def site_ground(elevation, slab_thickness, ground_level, on_ground)
        on_ground ? ground_level - elevation : -slab_thickness
      end

      # Storeys whose floor lies below the ground level: their footprints are
      # holes in the terrain.
      def basements(storeys, ground_level) = storeys.select { |s| s['elevation'] < ground_level - 1.0 }

      # Wall height of a storey whose height is given floor to floor (explicit
      # elevations): the walls stop under the next floor's slab.
      def wall_height(floor_to_floor, slab_thickness)
        h = floor_to_floor - slab_thickness
        unless h.positive?
          raise InvalidParams, "storey height #{Geometry.fmt(floor_to_floor)} mm floor to floor leaves no wall under a " \
                               "#{Geometry.fmt(slab_thickness)} mm slab"
        end

        h
      end
    end
  end
end
