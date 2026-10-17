# frozen_string_literal: true

require_relative 'core'
require_relative 'arc'
require_relative '../config'

module Plomada
  # Railings along a path (glass panels, posts and bars with a round handrail,
  # or a solid parapet) and the swept primitives they and other 0.5 parts
  # share: ribbons, horizontal tubes and vertical cylinders. Plain face loops
  # in mm, storey-local; every solid is closed.
  module Geometry
    module_function

    # --- primitives ---------------------------------------------------------------

    # A polyline moved +d+ to its left (negative: right), each vertex on the
    # intersection of its two moved edges (a mitre); an open end moves square.
    def offset_polyline(pts, d, closed: false)
      n = pts.size
      count = closed ? n : n - 1
      normals = (0...count).map { |i| left_normal(unit(sub(pts[(i + 1) % n], pts[i]))) }
      pts.each_index.map do |i|
        prev = closed ? normals[(i - 1) % count] : (i.zero? ? nil : normals[i - 1])
        nxt = closed ? normals[i % count] : (i == n - 1 ? nil : normals[i])
        prev ||= nxt
        nxt ||= prev
        m = add(prev, nxt)
        len = norm(m)
        next add(pts[i], scale(nxt, d)) if len <= 1e-9

        m = scale(m, 1.0 / len)
        cos_half = dot(m, nxt)
        add(pts[i], scale(m, d / [cos_half, 0.2].max))
      end
    end

    # The closed solid between the polyline moved +left+ and +right+ (signed
    # offsets, left > right) from z0 to z1. An open ribbon gets end caps; a
    # closed one is a ring.
    def ribbon_faces(pts, left, right, z0, z1, closed: false)
      l = offset_polyline(pts, left, closed: closed)
      r = offset_polyline(pts, right, closed: closed)
      if closed
        outer, inner = signed_area(pts).positive? ? [r, l] : [l, r]
        outer = outer.reverse if signed_area(outer).negative?
        inner = inner.reverse if signed_area(inner).positive?
        return region_prism(outer, [inner], z0, z1).map { |f| [f[:outer]] + f[:holes] }
      end

      ring = r + l.reverse
      ring = ring.reverse if signed_area(ring).negative?
      prism_faces(ring, z0, z1).map { |f| [f] }
    end

    # A horizontal tube of +radius+ along a polyline at height +z+, its rings
    # mitred at every vertex, capped at open ends. Returns quads and caps.
    def tube_faces(pts, radius, z, sides, closed: false)
      n = pts.size
      rings = pts.each_index.map do |i|
        a = closed || i.positive? ? pts[(i - 1) % n] : nil
        b = closed || i < n - 1 ? pts[(i + 1) % n] : nil
        din = a ? unit(sub(pts[i], a)) : unit(sub(b, pts[i]))
        dout = b ? unit(sub(b, pts[i])) : din
        m = add(left_normal(din), left_normal(dout))
        m = norm(m) <= 1e-9 ? left_normal(dout) : scale(m, 1.0 / norm(m))
        stretch = 1.0 / [dot(m, left_normal(dout)), 0.2].max
        (0...sides).map do |k|
          ang = 2.0 * Math::PI * k / sides
          side = radius * Math.cos(ang) * stretch
          [pts[i][0] + (m[0] * side), pts[i][1] + (m[1] * side), z + (radius * Math.sin(ang))]
        end
      end
      faces = []
      spans = closed ? n : n - 1
      spans.times do |i|
        r0 = rings[i]
        r1 = rings[(i + 1) % n]
        sides.times do |k|
          k1 = (k + 1) % sides
          faces << [r0[k], r0[k1], r1[k1], r1[k]]
        end
      end
      unless closed
        faces << rings[0].reverse
        faces << rings[-1]
      end
      faces
    end

    # A vertical prism over a regular polygon of +sides+ around +center+.
    def cylinder_faces(center, radius, sides, z0, z1)
      ring = (0...sides).map do |k|
        a = 2.0 * Math::PI * k / sides
        [center[0] + (radius * Math.cos(a)), center[1] + (radius * Math.sin(a))]
      end
      prism_faces(ring, z0, z1)
    end

    # Points every +max+ mm or less along a polyline, ends included, equally
    # spaced along its whole length: [[point, direction], ...].
    def stations_along(pts, max, closed: false)
      segs = (closed ? pts.size : pts.size - 1).times.map { |i| [pts[i], pts[(i + 1) % pts.size]] }
      total = segs.sum { |a, b| dist(a, b) }
      count = [(total / max - 1e-9).ceil, 1].max
      targets = (0..count).map { |k| total * k / count }
      targets.pop if closed
      out = []
      acc = 0.0
      seg_i = 0
      targets.each do |t|
        seg_i += 1 while seg_i < segs.size - 1 && acc + dist(*segs[seg_i]) < t - 1e-9 && (acc += dist(*segs[seg_i]))
        a, b = segs[seg_i]
        u = unit(sub(b, a))
        out << [add(a, scale(u, (t - acc).clamp(0.0, dist(a, b)))), u]
      end
      out
    end

    def polyline_length(pts, closed)
      (closed ? pts.size : pts.size - 1).times.sum { |i| dist(pts[i], pts[(i + 1) % pts.size]) }
    end

    # The point at length +s+ along a polyline.
    def point_at_length(pts, s, closed)
      (closed ? pts.size : pts.size - 1).times do |i|
        a = pts[i]
        b = pts[(i + 1) % pts.size]
        len = dist(a, b)
        return lerp2(a, b, s / len) if s <= len + 1e-9

        s -= len
      end
      closed ? pts[0] : pts[-1]
    end

    # --- railings -------------------------------------------------------------------

    # +rec+ is a normalized railing record, +slab_rings+ the plan rings of the
    # slab it stands on (to find which side "toward the slab" is), +base+ the
    # storey-local z of its foot. Returns {line:, parts: [{name:, material:, faces:}]}.
    def railing_parts(rec, slab_rings, base, config = CONFIG)
      path = Arc.polyline(rec['path'], closed: rec['closed'], config: config)
      side = toward(path, slab_rings)
      line = offset_polyline(path, side * rec['inset'], closed: rec['closed'])
      parts = case rec['style']
              when 'glass' then glass_railing(line, rec, base, config)
              when 'bars' then bar_railing(line, rec, base, config)
              else solid_railing(line, rec, base, side, config)
              end
      { line: line, parts: parts }
    end

    # +1 when the slab lies to the left of the path, -1 to its right; a path
    # with no slab under it insets to its left.
    def toward(path, rings)
      return 1.0 if rings.empty?

      a, b = path[0], path[1]
      mid = scale(add(a, b), 0.5)
      n = left_normal(unit(sub(b, a)))
      rings.any? { |r| point_in_polygon?(add(mid, scale(n, 20.0)), r) } ? 1.0 : -1.0
    end

    # Glass panels split at every facet and at most railing_panel_max_mm wide,
    # each a closed 12 mm slab mitred to its neighbours; with rail, a square
    # top rail on the glass.
    def glass_railing(line, rec, base, config)
      t = config[:railing_glass_mm] / 2.0
      rail = rec['rail'] ? config[:railing_rail_mm] : 0.0
      top = base + rec['height'] - rail
      l = offset_polyline(line, t, closed: rec['closed'])
      r = offset_polyline(line, -t, closed: rec['closed'])
      max = config[:railing_panel_max_mm]
      parts = []
      spans = rec['closed'] ? line.size : line.size - 1
      spans.times do |i|
        j = (i + 1) % line.size
        len = dist(line[i], line[j])
        count = [(len / max - 1e-9).ceil, 1].max
        count.times do |k|
          f0 = k.to_f / count
          f1 = (k + 1).to_f / count
          ring = [lerp2(r[i], r[j], f0), lerp2(r[i], r[j], f1), lerp2(l[i], l[j], f1), lerp2(l[i], l[j], f0)]
          ring = ring.reverse if signed_area(ring).negative?
          parts << { name: "vidrio_#{parts.size + 1}", material: 'MAT_vidrio', faces: prism_faces(ring, base, top) }
        end
      end
      if rec['rail']
        h = config[:railing_rail_mm] / 2.0
        parts << { name: 'baranda_sup', material: 'MAT_metal',
                   faces: ribbon_faces(line, h, -h, top, top + config[:railing_rail_mm], closed: rec['closed']).flatten(1) }
      end
      parts
    end

    def lerp2(a, b, f) = [a[0] + ((b[0] - a[0]) * f), a[1] + ((b[1] - a[1]) * f)]

    # Round posts at most railing_post_spacing_mm apart, a round handrail on
    # them and round balusters between them with railing_baluster_gap_mm of
    # clear gap.
    def bar_railing(line, rec, base, config)
      sides = config[:railing_round_sides]
      post_r = config[:railing_post_diameter_mm] / 2.0
      rail_r = config[:railing_handrail_diameter_mm] / 2.0
      bal_r = config[:railing_baluster_diameter_mm] / 2.0
      top = base + rec['height'] - rail_r
      posts = stations_along(line, config[:railing_post_spacing_mm], closed: rec['closed'])
      parts = posts.each_with_index.map do |(p, _), i|
        { name: "poste_#{i + 1}", material: 'MAT_metal', faces: cylinder_faces(p, post_r, sides, base, top) }
      end
      parts << { name: 'pasamanos', material: 'MAT_metal', faces: tube_faces(line, rail_r, top, sides, closed: rec['closed']) }
      gap = config[:railing_baluster_gap_mm]
      total = polyline_length(line, rec['closed'])
      spans = rec['closed'] ? posts.size : posts.size - 1
      step = total / spans
      count = 0
      spans.times do |k|
        clear = step - (2 * post_r)
        n = [((clear - gap) / (gap + (2 * bal_r)) - 1e-9).ceil, 0].max
        n.times do |m|
          s = (step * k) + post_r + ((clear - (n * 2 * bal_r)) / (n + 1) * (m + 1)) + (bal_r * ((2 * m) + 1))
          count += 1
          parts << { name: "barrote_#{count}", material: 'MAT_metal',
                     faces: cylinder_faces(point_at_length(line, s, rec['closed']), bal_r, 8, base + 100.0, top - rail_r) }
        end
      end
      parts
    end

    # A parapet railing_solid_mm thick whose outer face runs on the inset line.
    def solid_railing(line, rec, base, side, config)
      t = config[:railing_solid_mm]
      left, right = side.positive? ? [t, 0.0] : [0.0, -t]
      [{ name: 'parapeto', material: 'MAT_revoque_blanco',
         faces: ribbon_faces(line, left, right, base, base + rec['height'], closed: rec['closed']).flatten(1) }]
    end
  end
end
