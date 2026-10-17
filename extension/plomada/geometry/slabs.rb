# frozen_string_literal: true

require_relative 'core'
require_relative 'arc'
require_relative 'boolean'
require_relative '../config'

module Plomada
  # Slab records (0.5): every outline and hole tessellated with Arc, the
  # slabs that share a top elevation unioned into one region (so a balcony
  # overlapping the floor plate leaves no coplanar faces), the stair wells cut
  # out, and an edge band hanging under each slab that asks for one.
  module Geometry
    module_function

    # +slabs+ are normalized slab records, +wells+ plan rings to cut out.
    # Returns {slabs: [...], bands: [...], warnings: [...]}:
    #   slab: {top:, thickness:, material:, ids:, region:, faces:}
    #   band: {id:, top:, bottom:, region:, faces:}
    # with faces as {outer:, holes:, normal:} loops in mm, storey-local.
    def slab_layout(slabs, wells = [], config = CONFIG)
      warnings = []
      shapes = slabs.to_h { |s| [s['id'], slab_shape(s, config)] }
      groups = slabs.group_by { |s| s['top_offset'].round(3) }
      built = groups.map do |top, list|
        thickness = list.map { |s| s['thickness'] }.max
        if list.map { |s| s['thickness'] }.uniq.size > 1
          warnings << "slabs #{list.map { |s| s['id'] }.join(', ')} share the top #{fmt(top)} with different " \
                      "thicknesses; their union is #{fmt(thickness)} mm thick"
        end
        region = Boolean.region(list.map { |s| shapes[s['id']] }, wells)
        { top: top, thickness: thickness, material: list.first['material'], ids: list.map { |s| s['id'] },
          region: region, faces: region.flat_map { |r| region_prism(r[:outer], r[:holes], top - thickness, top) } }
      end
      bands = slabs.select { |s| s['edge_band'] }.filter_map do |s|
        band = edge_band(s, shapes, groups[s['top_offset'].round(3)], config)
        warnings << "slab #{s['id']}: its edge band of #{fmt(s['edge_band']['depth'])} mm does not fit" unless band
        band
      end
      { slabs: built, bands: bands, warnings: warnings }
    end

    def slab_shape(slab, config = CONFIG)
      tol = config[:arc_chord_tol_mm]
      { outer: ccw_ring(Arc.polyline(slab['outline'], closed: true, chord_tol: tol, config: config)),
        holes: slab['holes'].map { |h| ccw_ring(Arc.polyline(h, closed: true, chord_tol: tol, config: config)) } }
    end

    def ccw_ring(pts) = signed_area(pts).negative? ? pts.reverse : pts

    # The band under one slab: the strip between its outline and the outline
    # grown inward by the band depth, minus the slabs at the same top grown by
    # that depth (the band runs only along the free edge, never under or
    # along a neighbouring slab).
    def edge_band(slab, shapes, same_top, config = CONFIG)
      band = slab['edge_band']
      outer = shapes[slab['id']][:outer]
      inner = begin
        offset_polygon(outer, -band['depth'])
      rescue ArgumentError
        return nil
      end
      return nil if signed_area(inner) <= 0.0

      others = same_top.reject { |s| s['id'] == slab['id'] }.map do |s|
        offset_polygon(shapes[s['id']][:outer], band['depth'])
      rescue ArgumentError
        shapes[s['id']][:outer]
      end
      region = Boolean.region([{ outer: outer, holes: [] }], [inner] + others)
      return nil if region.empty?

      top = slab['top_offset'] - slab['thickness']
      bottom = top - band['height']
      { id: slab['id'], top: top, bottom: bottom, region: region,
        faces: region.flat_map { |r| region_prism(r[:outer], r[:holes], bottom, top) } }
    end

    # The closed prism of a region ring (counter-clockwise) with clockwise
    # holes, from z0 to z1: the same face layout as slab_faces, with no test
    # that throws holes away.
    def region_prism(outer, holes, z0, z1)
      lift = ->(ring, z) { ring.map { |p| [p[0], p[1], z] } }
      ccw_holes = holes.map { |h| signed_area(h).negative? ? h.reverse : h }
      faces = [
        { outer: lift.call(outer, z1), holes: ccw_holes.map { |h| lift.call(h.reverse, z1) }, normal: [0.0, 0.0, 1.0] },
        { outer: lift.call(outer.reverse, z0), holes: ccw_holes.map { |h| lift.call(h, z0) }, normal: [0.0, 0.0, -1.0] }
      ]
      side = lambda do |ring, inward|
        ring.each_index do |i|
          p = ring[i]
          q = ring[(i + 1) % ring.size]
          quad = [[p[0], p[1], z0], [q[0], q[1], z0], [q[0], q[1], z1], [p[0], p[1], z1]]
          quad = quad.reverse if inward
          faces << { outer: quad, holes: [], normal: unit3(newell(quad)) }
        end
      end
      side.call(outer, false)
      ccw_holes.each { |h| side.call(h, true) }
      faces
    end
  end
end
