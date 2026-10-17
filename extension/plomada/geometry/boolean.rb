# frozen_string_literal: true

require_relative 'core'
require_relative '../config'

module Plomada
  module Geometry
    # Plane regions as unions of shapes minus cuts, by edge classification:
    # every edge of every ring is split where it meets another, each piece is
    # probed a hair to its left and right, and a piece is kept as boundary
    # when exactly one side lies inside the region, oriented with the region
    # on its left. Chaining the kept pieces gives the outer rings
    # (counter-clockwise) and their holes (clockwise). No sweep line, no
    # special cases for touching or overlapping input: shapes that share an
    # edge simply lose it.
    #
    # Shapes are {outer: ring, holes: [ring, ...]}; a point is in a shape when
    # it is inside the outer ring and in none of its holes, and in the region
    # when it is in some shape and in no cut. Rings may wind either way.
    module Boolean
      module_function

      # Returns [{outer: [[x, y]...], holes: [[[x, y]...]]}], largest first.
      def region(shapes, cuts = [], probe: CONFIG[:boolean_probe_mm], weld: 1e-4)
        rings = shapes.flat_map { |s| [s[:outer]] + (s[:holes] || []) } + cuts
        pool = Pool.new(weld)
        edges = split_edges(rings.map { |r| clean(r) }.reject { |r| r.size < 3 }, pool)
        kept = edges.filter_map do |a, b|
          p = pool[a]
          q = pool[b]
          d = Geometry.sub(q, p)
          len = Geometry.norm(d)
          next if len <= 1e-9

          # A piece shorter than the probe is probed a quarter of its length off.
          mid = Geometry.scale(Geometry.add(p, q), 0.5)
          n = Geometry.scale(Geometry.left_normal(d), [probe, len / 4.0].min / len)
          left = inside?(Geometry.add(mid, n), shapes, cuts)
          right = inside?(Geometry.sub(mid, n), shapes, cuts)
          next if left == right

          left ? [a, b] : [b, a]
        end
        assemble(chain(kept, pool), pool)
      end

      def inside?(pt, shapes, cuts)
        return false if cuts.any? { |c| Geometry.point_in_polygon?(pt, c) }

        shapes.any? do |s|
          Geometry.point_in_polygon?(pt, s[:outer]) && (s[:holes] || []).none? { |h| Geometry.point_in_polygon?(pt, h) }
        end
      end

      def clean(ring) = Geometry.dedupe_ring(ring.map { |p| [p[0].to_f, p[1].to_f] }, 1e-6)

      # Welds points closer than +weld+ so a crossing computed from two edges
      # is one vertex.
      class Pool
        def initialize(weld)
          @weld = weld
          @points = []
          @grid = {}
        end

        def [](i) = @points[i]

        def id(p)
          kx = (p[0] / @weld).floor
          ky = (p[1] / @weld).floor
          (-1..1).each do |dx|
            (-1..1).each do |dy|
              (@grid[[kx + dx, ky + dy]] || []).each do |i|
                q = @points[i]
                return i if (q[0] - p[0]).abs <= @weld && (q[1] - p[1]).abs <= @weld
              end
            end
          end
          @points << p
          (@grid[[kx, ky]] ||= []) << (@points.size - 1)
          @points.size - 1
        end
      end

      # Every ring edge split at every point where another edge crosses or
      # touches it, as unique undirected [id, id] pairs.
      def split_edges(rings, pool)
        segs = rings.flat_map { |r| r.each_index.map { |i| [r[i], r[(i + 1) % r.size]] } }
        boxes = segs.map { |p, q| [[p[0], q[0]].min, [p[1], q[1]].min, [p[0], q[0]].max, [p[1], q[1]].max] }
        cuts = segs.map { |_| [0.0, 1.0] }
        segs.each_index do |i|
          (i + 1...segs.size).each do |j|
            a = boxes[i]
            b = boxes[j]
            next if a[0] > b[2] + 1e-6 || b[0] > a[2] + 1e-6 || a[1] > b[3] + 1e-6 || b[1] > a[3] + 1e-6

            meet(segs[i], segs[j]).each do |ti, tj|
              cuts[i] << ti
              cuts[j] << tj
            end
          end
        end
        out = {}
        segs.each_with_index do |(p, q), i|
          ids = cuts[i].uniq.sort.map { |t| pool.id(lerp(p, q, t)) }
          ids.chunk_while { |x, y| x == y }.map(&:first).each_cons(2) do |a, b|
            out[[a, b].minmax] ||= [a, b] unless a == b
          end
        end
        out.values
      end

      def lerp(p, q, t)
        return p if t <= 0.0
        return q if t >= 1.0

        [p[0] + ((q[0] - p[0]) * t), p[1] + ((q[1] - p[1]) * t)]
      end

      # Parameters [t on s1, t on s2] where two segments meet: a proper
      # crossing, an end touching the other segment, or the end points of a
      # collinear overlap.
      def meet(s1, s2, eps = 1e-9)
        p, q = s1
        r, s = s2
        d1 = Geometry.sub(q, p)
        d2 = Geometry.sub(s, r)
        den = Geometry.cross(d1, d2)
        l1 = Geometry.dot(d1, d1)
        l2 = Geometry.dot(d2, d2)
        return [] if l1 <= eps || l2 <= eps

        w = Geometry.sub(r, p)
        if den.abs <= 1e-9 * Math.sqrt(l1 * l2)
          return [] if (Geometry.cross(w, d1).abs / Math.sqrt(l1)) > 1e-6 # parallel, apart

          # Collinear: each end of one inside the other.
          out = []
          [r, s].each_with_index do |pt, k|
            t = Geometry.dot(Geometry.sub(pt, p), d1) / l1
            out << [t, k.to_f] if t > eps && t < 1.0 - eps
          end
          [p, q].each_with_index do |pt, k|
            t = Geometry.dot(Geometry.sub(pt, r), d2) / l2
            out << [k.to_f, t] if t > eps && t < 1.0 - eps
          end
          return out
        end

        t = Geometry.cross(w, d2) / den
        u = Geometry.cross(w, d1) / den
        tol = 1e-9
        return [] if t < -tol || t > 1.0 + tol || u < -tol || u > 1.0 + tol

        [[t.clamp(0.0, 1.0), u.clamp(0.0, 1.0)]]
      end

      # Kept directed edges into closed loops of ids. At a vertex where two
      # loops touch, the walk takes the sharpest left turn, so each loop stays
      # simple.
      def chain(edges, pool)
        out = Hash.new { |h, k| h[k] = [] }
        edges.each { |a, b| out[a] << b }
        loops = []
        until out.empty?
          start, nexts = out.first
          first = nexts.shift
          out.delete(start) if nexts.empty?
          loop_ids = [start]
          prev = start
          cur = first
          guard = edges.size + 1
          while cur != start && guard.positive?
            guard -= 1
            loop_ids << cur
            cands = out[cur]
            break if cands.empty?

            nxt = pick(pool, prev, cur, cands)
            cands.delete_at(cands.index(nxt))
            out.delete(cur) if cands.empty?
            prev = cur
            cur = nxt
          end
          loops << loop_ids if cur == start && loop_ids.size >= 3
        end
        loops
      end

      def pick(pool, prev, cur, cands)
        return cands.first if cands.size == 1

        din = Geometry.sub(pool[cur], pool[prev])
        cands.max_by do |c|
          dout = Geometry.sub(pool[c], pool[cur])
          Math.atan2(Geometry.cross(din, dout), Geometry.dot(din, dout))
        end
      end

      def assemble(loops, pool)
        # Pieces split where edges cross can leave vertices microns apart; they carry no shape.
        rings = loops.map { |l| Geometry.dedupe_ring(l.map { |i| pool[i] }, 0.05) }.select { |r| r.size >= 3 }
        outers, holes = rings.partition { |r| Geometry.signed_area(r).positive? }
        result = outers.sort_by { |r| -Geometry.signed_area(r) }.map { |r| { outer: r, holes: [] } }
        holes.each do |h|
          probe = interior_point(h)
          owner = result.select { |o| Geometry.point_in_polygon?(probe, o[:outer]) }
                        .min_by { |o| Geometry.signed_area(o[:outer]) }
          owner[:holes] << h if owner
        end
        result
      end

      # A point just inside a clockwise hole ring, next to its first edge.
      def interior_point(ring)
        p = ring[0]
        q = ring[1]
        d = Geometry.sub(q, p)
        mid = Geometry.scale(Geometry.add(p, q), 0.5)
        # A hole runs clockwise: the region is on its left, the hole on its right.
        Geometry.sub(mid, Geometry.scale(Geometry.left_normal(d), 1e-3 / Geometry.norm(d)))
      end

      def area(region) = region.sum { |r| Geometry.signed_area(r[:outer]) + r[:holes].sum { |h| Geometry.signed_area(h) } }
    end
  end
end
