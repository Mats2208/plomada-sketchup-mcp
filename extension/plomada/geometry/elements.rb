# frozen_string_literal: true

require_relative 'core'
require_relative 'arc'
require_relative 'boolean'
require_relative 'slabs'
require_relative 'railings'
require_relative 'site'
require_relative '../config'

module Plomada
  # Columns, pools on any storey and ramps, as closed face loops in mm,
  # storey-local.
  module Geometry
    module_function

    # --- columns ----------------------------------------------------------------------

    # Sides of a round column: as many as the chord tolerance asks for its
    # radius, clamped to column_min_sides..column_max_sides.
    def column_sides(radius, config = CONFIG)
      tol = config[:arc_chord_tol_mm]
      step = tol >= radius ? Math::PI : 2.0 * Math.acos(1.0 - (tol / radius))
      (2.0 * Math::PI / step).ceil.clamp(config[:column_min_sides], config[:column_max_sides])
    end

    # A column record from z0 to z1: a polygon prism (round) or its outline's.
    def column_faces(rec, z0, z1, config = CONFIG)
      if rec['outline']
        ring = ccw_ring(Arc.polyline(rec['outline'], closed: true, config: config))
        return prism_faces(ring, z0, z1)
      end

      r = rec['diameter'] / 2.0
      cylinder_faces(rec['at'], r, column_sides(r, config), z0, z1)
    end

    # --- pools ------------------------------------------------------------------------

    # A pool record standing at +base+ (storey-local z of the floor or slab it
    # is on). Recessed: the 0.4 pileta sunk below +base+. Raised: a basin of
    # pool_raised_wall_mm walls on the slab, its floor on the slab, the water
    # surface +depth+ above the slab, a coping on the wall tops.
    # Returns {parts: {name => [faces, material, holed]}, outer:, raised:}.
    def pool_layout(rec, base, raised, config = CONFIG)
      inner = ccw_ring(Arc.polyline(rec['outline'], closed: true, config: config))
      unless raised
        parts = pool_parts(inner, base, rec['depth'], coping: rec['coping'])
        return { raised: false, outer: parts[:outer],
                 parts: { 'borde' => [parts[:coping], 'MAT_borde', true], 'vaso' => [parts[:walls], 'MAT_revoque_blanco', true],
                          'fondo' => [parts[:floor], 'MAT_revoque_blanco', false], 'agua' => [parts[:water], 'MAT_agua', false] } }
      end

      wall = config[:pool_raised_wall_mm]
      outer_wall = offset_polygon(inner, wall)
      floor_top = base + POOL_FLOOR_MM
      water = base + rec['depth']
      top = water + POOL_WATER_DROP_MM
      parts = {
        'vaso' => [region_prism(outer_wall, [inner.reverse], base, top), 'MAT_revoque_blanco', true],
        'fondo' => [prism_faces(inner, base, floor_top), 'MAT_revoque_blanco', false],
        'agua' => [prism_faces(inner, floor_top, water), 'MAT_agua', false]
      }
      outer = outer_wall
      if rec['coping'].positive?
        outer = offset_polygon(inner, wall + rec['coping'])
        parts['borde'] = [region_prism(outer, [inner.reverse], top, top + 30.0), 'MAT_borde', true]
      end
      { raised: true, outer: outer, parts: parts }
    end

    # --- ramps ------------------------------------------------------------------------

    # The closed solid between the polyline moved +left+ and +right+, whose
    # bottom and top follow per-vertex heights +lo+ and +hi+: a sloped ribbon.
    # Top and bottom are triangulated (a warped quad is not planar).
    def sloped_ribbon_faces(pts, left, right, lo, hi)
      l = offset_polyline(pts, left)
      r = offset_polyline(pts, right)
      at = ->(p, z) { [p[0], p[1], z] }
      faces = []
      (pts.size - 1).times do |i|
        j = i + 1
        faces << [at.call(r[i], hi[i]), at.call(r[j], hi[j]), at.call(l[j], hi[j])]
        faces << [at.call(r[i], hi[i]), at.call(l[j], hi[j]), at.call(l[i], hi[i])]
        faces << [at.call(r[i], lo[i]), at.call(l[j], lo[j]), at.call(r[j], lo[j])]
        faces << [at.call(r[i], lo[i]), at.call(l[i], lo[i]), at.call(l[j], lo[j])]
        faces << [at.call(r[i], lo[i]), at.call(r[j], lo[j]), at.call(r[j], hi[j]), at.call(r[i], hi[i])]
        faces << [at.call(l[j], lo[j]), at.call(l[i], lo[i]), at.call(l[i], hi[i]), at.call(l[j], hi[j])]
      end
      faces << [at.call(l[0], lo[0]), at.call(r[0], lo[0]), at.call(r[0], hi[0]), at.call(l[0], hi[0])]
      faces << [at.call(r[-1], lo[-1]), at.call(l[-1], lo[-1]), at.call(l[-1], hi[-1]), at.call(r[-1], hi[-1])]
      faces
    end

    # A ramp record: its surface from z_start to z_end along the axis (ruled
    # across the width, ramp_thickness_mm thick), and side walls of
    # ramp_wall_mm whose tops follow the slope wall_height above it.
    # Returns {parts: {name => faces}, outline:, slope:}.
    def ramp_layout(rec, config = CONFIG)
      axis = Arc.polyline(rec['axis'], config: config)
      lengths = [0.0]
      axis.each_cons(2) { |a, b| lengths << (lengths[-1] + dist(a, b)) }
      total = lengths[-1]
      z = lengths.map { |s| rec['z_start'] + ((rec['z_end'] - rec['z_start']) * s / total) }
      h = rec['width'] / 2.0
      t = config[:ramp_thickness_mm]
      parts = { 'rampa' => sloped_ribbon_faces(axis, h, -h, z.map { |v| v - t }, z) }
      outer = h
      if rec['side_walls']
        w = config[:ramp_wall_mm]
        hi = z.map { |v| v + rec['wall_height'] }
        lo = z.map { |v| v - t }
        parts['muro_izq'] = sloped_ribbon_faces(axis, h + w, h, lo, hi)
        parts['muro_der'] = sloped_ribbon_faces(axis, -h, -h - w, lo, hi)
        outer = h + w
      end
      ring = offset_polyline(axis, -outer) + offset_polyline(axis, outer).reverse
      ring = ring.reverse if signed_area(ring).negative?
      run = (rec['axis'].size - 1).times.sum do |i|
        Arc.length(rec['axis'][i][0, 2], rec['axis'][i + 1][0, 2], Arc.bulge_of(rec['axis'][i]))
      end
      { parts: parts, outline: ring, slope: (rec['z_end'] - rec['z_start']).abs / run }
    end
  end
end
