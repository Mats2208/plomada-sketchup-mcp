# frozen_string_literal: true

require 'zlib'
require_relative 'core'
require_relative 'arc'
require_relative 'boolean'
require_relative 'slabs'
require_relative '../config'

module Plomada
  # Planters: a curb ring, the soil inside it and plants scattered over the
  # soil by Bridson's Poisson-disk sampling, so no two plants stand closer
  # than the spacing and a rebuild puts every plant back where it was.
  module Geometry
    module_function

    # +rec+ a normalized planter record, +base+ the storey-local z of its foot.
    # Returns {curb:, soil:, plants: [{at: [x, y], z:, key:}], inner:}.
    def planter_layout(rec, base, config = CONFIG)
      outer = ccw_ring(Arc.polyline(rec['outline'], closed: true, config: config))
      inner = begin
        offset_polygon(outer, -rec['curb_thickness'])
      rescue ArgumentError
        nil
      end
      if inner.nil? || signed_area(inner) <= 0.0
        raise InvalidParams, "planter #{rec['id']}: a #{fmt(rec['curb_thickness'])} mm curb leaves no soil inside its outline"
      end

      curb = Boolean.region([{ outer: outer, holes: [] }], [inner])
      plants = []
      if rec['plants']
        p = rec['plants']
        seed = p['seed'] + p['spacing'].round + Zlib.crc32(rec['id'].to_s)
        pts = poisson_disk(inner, p['spacing'], seed, config[:planter_attempts])
        rng = Random.new(seed)
        plants = pts.map do |pt|
          { at: pt, z: base + rec['soil_level'], key: p['keys'][rng.rand(p['keys'].size)], rotation: rng.rand * 360.0 }
        end
      end
      {
        curb: curb.flat_map { |r| region_prism(r[:outer], r[:holes], base, base + rec['height']) },
        soil: prism_faces(inner, base, base + rec['soil_level']),
        plants: plants, inner: inner
      }
    end

    # Bridson's algorithm inside +poly+: a background grid of cell r / sqrt(2)
    # holds at most one sample per cell; from each active sample up to +k+
    # candidates are drawn in the annulus [r, 2r] and kept when inside the
    # polygon and at least +r+ from every sample. Deterministic for a seed.
    def poisson_disk(poly, r, seed, k = CONFIG[:planter_attempts])
      rng = Random.new(seed)
      xs = poly.map { |p| p[0] }
      ys = poly.map { |p| p[1] }
      x0 = xs.min
      y0 = ys.min
      cell = r / Math.sqrt(2.0)
      grid = {}
      key = ->(p) { [((p[0] - x0) / cell).floor, ((p[1] - y0) / cell).floor] }
      fits = lambda do |p|
        return false unless point_in_polygon?(p, poly)

        gx, gy = key.call(p)
        (-2..2).none? do |dx|
          (-2..2).any? do |dy|
            q = grid[[gx + dx, gy + dy]]
            q && dist(p, q) < r
          end
        end
      end
      first = nil
      200.times do
        c = [x0 + (rng.rand * (xs.max - x0)), y0 + (rng.rand * (ys.max - y0))]
        next unless point_in_polygon?(c, poly)

        first = c
        break
      end
      first ||= centroid(poly)
      return [] unless point_in_polygon?(first, poly)

      samples = [first]
      grid[key.call(first)] = first
      active = [first]
      until active.empty?
        i = rng.rand(active.size)
        base = active[i]
        found = false
        k.times do
          ang = rng.rand * 2.0 * Math::PI
          rad = r * (1.0 + rng.rand)
          c = [base[0] + (rad * Math.cos(ang)), base[1] + (rad * Math.sin(ang))]
          next unless fits.call(c)

          samples << c
          grid[key.call(c)] = c
          active << c
          found = true
          break
        end
        active.delete_at(i) unless found
      end
      samples
    end

    # A closed lat-long sphere standing in for a plant the library lacks.
    def sphere_faces(center, radius, z_base, rings = 6, segments = 10)
      cz = z_base + radius
      pt = lambda do |i, j|
        phi = Math::PI * i / rings
        th = 2.0 * Math::PI * j / segments
        [center[0] + (radius * Math.sin(phi) * Math.cos(th)), center[1] + (radius * Math.sin(phi) * Math.sin(th)),
         cz - (radius * Math.cos(phi))]
      end
      faces = []
      rings.times do |i|
        segments.times do |j|
          j1 = (j + 1) % segments
          if i.zero?
            faces << [pt.call(0, 0), pt.call(1, j1), pt.call(1, j)]
          elsif i == rings - 1
            faces << [pt.call(i, j), pt.call(i, j1), pt.call(rings, 0)]
          else
            faces << [pt.call(i, j), pt.call(i, j1), pt.call(i + 1, j1), pt.call(i + 1, j)]
          end
        end
      end
      faces
    end
  end
end
