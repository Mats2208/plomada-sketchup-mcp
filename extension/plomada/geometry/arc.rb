# frozen_string_literal: true

require_relative 'core'
require_relative '../config'

module Plomada
  module Geometry
    # Circular arcs given the DXF LWPOLYLINE way: two end points and a bulge,
    # tan(theta / 4) of the swept angle theta, positive counter-clockwise.
    # The one kernel every curved element goes through: walls, slab edges,
    # railings, planters, pools, ramps, paving, columns and terrain holes all
    # tessellate here with the same chord tolerance, so two elements that
    # share an arc share its vertices exactly.
    module Arc
      MAX_BULGE = 1.0 # a semicircle

      module_function

      # Swept angle in radians, signed (positive counter-clockwise).
      def sweep(bulge) = 4.0 * Math.atan(bulge.to_f)

      def straight?(bulge) = bulge.nil? || bulge.to_f.abs <= 1e-12

      # [center, radius] of the arc from p0 to p1. The center lies on the left
      # of the chord for a counter-clockwise arc under 180 degrees.
      def center_radius(p0, p1, bulge)
        b = bulge.to_f
        raise ArgumentError, 'a bulge of 0 is a straight segment, it has no center' if straight?(b)

        chord = Geometry.sub(p1, p0)
        c = Geometry.norm(chord)
        raise ArgumentError, 'an arc needs two distinct end points' if c <= EPS

        mid = Geometry.scale(Geometry.add(p0, p1), 0.5)
        u = Geometry.scale(chord, 1.0 / c)
        off = (c / 2.0) * (1.0 - (b * b)) / (2.0 * b)
        [Geometry.add(mid, Geometry.scale(Geometry.left_normal(u), off)), c * (1.0 + (b * b)) / (4.0 * b.abs)]
      end

      # Everything the tessellators and the wall solver need, computed once:
      # {c:, r:, a0:, sigma:, sweep: (absolute, radians), len:}.
      def frame(p0, p1, bulge)
        c, r = center_radius(p0, p1, bulge)
        theta = sweep(bulge)
        { c: c, r: r, a0: Math.atan2(p0[1] - c[1], p0[0] - c[0]), sigma: theta.positive? ? 1.0 : -1.0,
          sweep: theta.abs, len: r * theta.abs }
      end

      def length(p0, p1, bulge)
        return Geometry.dist(p0, p1) if straight?(bulge)

        frame(p0, p1, bulge)[:len]
      end

      # The point at arc length +s+ from p0 (s may run past either end: the
      # circle continues).
      def point_at(p0, p1, bulge, s)
        return Geometry.add(p0, Geometry.scale(Geometry.unit(Geometry.sub(p1, p0)), s)) if straight?(bulge)

        f = frame(p0, p1, bulge)
        at_angle(f, f[:a0] + (f[:sigma] * s / f[:r]), f[:r])
      end

      # Unit tangent at arc length +s+, pointing from p0 toward p1.
      def tangent_at(p0, p1, bulge, s)
        return Geometry.unit(Geometry.sub(p1, p0)) if straight?(bulge)

        f = frame(p0, p1, bulge)
        a = f[:a0] + (f[:sigma] * s / f[:r])
        [-Math.sin(a) * f[:sigma], Math.cos(a) * f[:sigma]]
      end

      def at_angle(f, angle, radius) = [f[:c][0] + (radius * Math.cos(angle)), f[:c][1] + (radius * Math.sin(angle))]

      # The concentric arc +d+ mm to the left of the arc (toward the center of
      # a counter-clockwise arc): radius r - d for a counter-clockwise arc,
      # r + d for a clockwise one. Same bulge. Returns [q0, q1, bulge].
      def offset(p0, p1, bulge, d, config = CONFIG)
        f = frame(p0, p1, bulge)
        radius = f[:r] - (f[:sigma] * d)
        min = config[:arc_min_offset_radius_mm]
        if radius < min
          raise InvalidParams, "an offset of #{Geometry.fmt(d)} mm from an arc of radius #{Geometry.fmt(f[:r])} mm " \
                               "leaves radius #{Geometry.fmt(radius)} mm, under the #{Geometry.fmt(min)} mm minimum"
        end

        a1 = f[:a0] + (f[:sigma] * f[:sweep])
        [at_angle(f, f[:a0], radius), at_angle(f, a1, radius), bulge.to_f]
      end

      # Facets for an arc of +radius+ sweeping +angle+ (radians): the fewest
      # whose sagitta r (1 - cos(a / 2)) stays within +tol+, clamped to
      # arc_min_facets..arc_max_facets.
      def facets(radius, angle, tol = nil, config = CONFIG)
        tol ||= config[:arc_chord_tol_mm]
        step = tol >= radius ? Math::PI : 2.0 * Math.acos(1.0 - (tol / radius))
        n = (angle.abs / step).ceil
        n.clamp(config[:arc_min_facets], config[:arc_max_facets])
      end

      # Points from p0 to p1 (both included, exactly as given) at equal angles,
      # whose maximum sagitta is +chord_tol+. A straight segment gives [p0, p1].
      def tessellate(p0, p1, bulge, chord_tol = nil, config = CONFIG)
        return [p0, p1] if straight?(bulge)

        f = frame(p0, p1, bulge)
        n = facets(f[:r], f[:sweep], chord_tol, config)
        inner = (1...n).map { |k| at_angle(f, f[:a0] + (f[:sigma] * f[:sweep] * k / n), f[:r]) }
        [p0] + inner + [p1]
      end

      # Refuses a bulge above a semicircle, naming the segment.
      def check_bulge!(bulge, index, owner)
        return if bulge.nil? || bulge.to_f.abs <= MAX_BULGE + 1e-9

        deg = sweep(bulge).abs * 180.0 / Math::PI
        raise InvalidParams, "segment #{index} of #{owner} sweeps #{Geometry.fmt(deg.round(3))} degrees; " \
                             'split arcs above 180 degrees'
      end

      def bulge_of(vertex) = vertex.size > 2 ? vertex[2].to_f : 0.0

      # A polyline of [x, y] or [x, y, bulge] vertices as plain [x, y] points,
      # every arc tessellated. Closed: the last vertex's bulge closes the ring
      # and the first point is not repeated.
      def polyline(vertices, closed: false, chord_tol: nil, config: CONFIG)
        pts = vertices.map { |v| [v[0].to_f, v[1].to_f] }
        count = closed ? pts.size : pts.size - 1
        out = []
        (0...count).each do |i|
          a = pts[i]
          b = pts[(i + 1) % pts.size]
          seg = tessellate(a, b, bulge_of(vertices[i]), chord_tol, config)
          out.concat(seg[0..-2])
        end
        out << pts[-1] unless closed
        out
      end

      # True when any vertex carries a non-zero bulge.
      def curved?(vertices) = vertices.any? { |v| !straight?(bulge_of(v)) }
    end
  end
end
