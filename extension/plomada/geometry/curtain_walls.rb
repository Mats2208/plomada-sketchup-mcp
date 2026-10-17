# frozen_string_literal: true

require_relative 'core'
require_relative 'arc'
require_relative 'railings'
require_relative '../config'

module Plomada
  # Curtain walls: mullions dividing each path segment equally at most
  # mullion_spacing apart, sill and head rails, transoms and flat glass between
  # them; and, where a curtain wall runs inside a wall of the plan, the
  # full-height void it cuts there, handed to the wall solver as an opening
  # without a component.
  module Geometry
    module_function

    # Segments of a polyline record as [a, b, bulge].
    def record_segments(vertices, closed)
      count = closed ? vertices.size : vertices.size - 1
      (0...count).map do |i|
        [vertices[i][0, 2].map(&:to_f), vertices[(i + 1) % vertices.size][0, 2].map(&:to_f), Arc.bulge_of(vertices[i])]
      end
    end

    # Mullion points along the path: every segment divided equally into
    # spans no longer than +spacing+, shared vertices once.
    def mullion_points(rec, spacing)
      pts = []
      record_segments(rec['path'], rec['closed']).each do |a, b, bulge|
        len = Arc.length(a, b, bulge)
        n = [(len / spacing - 1e-9).ceil, 1].max
        (0..n).each do |k|
          p = k == n ? b : Arc.point_at(a, b, bulge, len * k / n)
          pts << p unless pts.any? && dist(pts[-1], p) < 1e-6
        end
      end
      pts.pop if rec['closed'] && dist(pts[0], pts[-1]) < 1e-6
      pts
    end

    def oriented_box(p, q, depth, z0, z1)
      u = unit(sub(q, p))
      n = scale(left_normal(u), depth / 2.0)
      ring = [sub(p, n), sub(q, n), add(q, n), add(p, n)]
      prism_faces(ring, z0, z1)
    end

    # +head+ is the record's head or the storey's wall height. Returns
    # {parts: [{name:, material:, faces:}], mullions: count, panes: count}.
    def curtain_wall_parts(rec, head, config = CONFIG)
      w, d = rec['profile']
      sill = rec['sill']
      pts = mullion_points(rec, rec['mullion_spacing'])
      spans = rec['closed'] ? pts.size : pts.size - 1
      parts = []
      pts.each_with_index do |p, i|
        prev = pts[(i - 1) % pts.size] if rec['closed'] || i.positive?
        nxt = pts[(i + 1) % pts.size] if rec['closed'] || i < pts.size - 1
        u = unit(add(prev ? unit(sub(p, prev)) : [0.0, 0.0], nxt ? unit(sub(nxt, p)) : [0.0, 0.0]))
        a = sub(p, scale(u, w / 2.0))
        b = add(p, scale(u, w / 2.0))
        parts << { name: "montante_#{i + 1}", material: 'MAT_metal', faces: oriented_box(a, b, d, sill, head) }
      end
      levels = ([sill + (w / 2.0)] + rec['transoms'].select { |z| z > sill + w && z < head - w } + [head - (w / 2.0)]).uniq
      panes = 0
      spans.times do |i|
        p = pts[i]
        q = pts[(i + 1) % pts.size]
        u = unit(sub(q, p))
        a = add(p, scale(u, w / 2.0))
        b = sub(q, scale(u, w / 2.0))
        levels.each_with_index do |z, k|
          parts << { name: "travesano_#{i + 1}_#{k + 1}", material: 'MAT_metal',
                     faces: oriented_box(a, b, d, z - (w / 2.0), z + (w / 2.0)) }
        end
        levels.each_cons(2) do |z0, z1|
          panes += 1
          parts << { name: "vidrio_#{panes}", material: 'MAT_vidrio',
                     faces: oriented_box(a, b, rec['glass'], z0 + (w / 2.0), z1 - (w / 2.0)) }
        end
      end
      { parts: parts, mullions: pts.size, panes: panes }
    end

    # The voids curtain walls cut in the plan's walls: for every curtain path
    # segment lying on a wall axis segment (both ends on it, the same line or
    # circle), an opening record on that wall over the segment's arc length,
    # from the curtain's sill to its head, with no component. Segments that
    # run free of walls cut nothing.
    def curtain_voids(walls, curtains, height, config = CONFIG)
      tol = config[:junction_tolerance_mm]
      curtains.flat_map do |c|
        record_segments(c['path'], c['closed']).each_with_index.filter_map do |(a, b, bulge), i|
          hit = curtain_host(walls, a, b, bulge, tol)
          next unless hit

          wall, s0, s1, wall_height = hit
          head = c['head'] || wall_height || height
          { 'id' => "#{c['id']}_#{i}", 'wall' => wall, 'opening_kind' => 'window', 'offset' => s0, 'width' => s1 - s0,
            'sill' => c['sill'], 'height' => head - c['sill'], 'swing' => 'in', 'hand' => 'left', 'tag' => c['id'],
            'curtain' => c['id'] }
        end
      end
    end

    # [wall id, s0, s1, wall height] of the wall axis segment the curtain
    # segment a-b lies on, s measured along that wall's whole axis; nil if none.
    def curtain_host(walls, a, b, bulge, tol)
      walls.each do |w|
        base = 0.0
        record_segments(w['axis'], w['closed']).each do |wa, wb, wbulge|
          len = Arc.length(wa, wb, wbulge)
          if Arc.straight?(wbulge) == Arc.straight?(bulge)
            sa = on_segment(wa, wb, wbulge, len, a, tol)
            sb = on_segment(wa, wb, wbulge, len, b, tol)
            same = Arc.straight?(bulge) || same_circle?(wa, wb, wbulge, a, b, bulge, tol)
            return [w['id'], base + [sa, sb].min, base + [sa, sb].max, w['height']] if sa && sb && same
          end
          base += len
        end
      end
      nil
    end

    # Arc length of point +p+ along the segment when it lies on it, else nil.
    def on_segment(a, b, bulge, len, p, tol)
      if Arc.straight?(bulge)
        u = unit(sub(b, a))
        s = dot(sub(p, a), u)
        off = cross(u, sub(p, a)).abs
        return off <= tol && s >= -tol && s <= len + tol ? s.clamp(0.0, len) : nil
      end

      f = Arc.frame(a, b, bulge)
      return nil if (dist(p, f[:c]) - f[:r]).abs > tol

      d = (Math.atan2(p[1] - f[:c][1], p[0] - f[:c][0]) - f[:a0]) * f[:sigma]
      d -= 2.0 * Math::PI * ((d - (f[:sweep] / 2.0) + Math::PI) / (2.0 * Math::PI)).floor
      s = d * f[:r]
      s >= -tol && s <= len + tol ? s.clamp(0.0, len) : nil
    end

    def same_circle?(a, b, bulge, c, d, bulge2, tol)
      c1, r1 = Arc.center_radius(a, b, bulge)
      c2, r2 = Arc.center_radius(c, d, bulge2)
      dist(c1, c2) <= tol * 10 && (r1 - r2).abs <= tol * 10
    end

    # The openings a storey's walls are cut with: its doors and windows and
    # the voids of its curtain walls.
    def wall_openings(plan, height)
      plan['openings'] + curtain_voids(plan['walls'], plan['curtain_walls'] || [], height)
    end
  end
end
