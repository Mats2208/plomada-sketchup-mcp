# frozen_string_literal: true

require_relative 'core'
require_relative 'mesh'
require_relative '../config'

module Plomada
  module Geometry
    # Turns plan walls and openings into the closed boundary of the walls, as
    # faces ready to build, with no booleans.
    #
    # The method, in order:
    # 1. Each wall axis is split into straight segments. A segment's two face
    #    lines sit at signed offsets off_l and off_r along its left normal
    #    (center: +t/2 and -t/2; left: +t and 0; right: 0 and -t).
    # 2. Every segment end gets a cut: an s value on each face line. Polyline
    #    vertices and two walls ending at one point are mitred at the
    #    intersection of the offset lines; junction angles under the minimum
    #    are refused. A wall end lying on another wall's axis is a T: the stem
    #    is trimmed to the through wall's near face (butt-joint pullback). Two
    #    axes crossing are an X: the thinner wall is cut at both faces of the
    #    thicker one, which runs through.
    # 3. Openings map onto segments by their accumulated offset; one that
    #    spans a corner, crosses into a joint, hits a junction or overlaps
    #    another opening is refused by id.
    # 4. Each segment is described in its own frame (s along the axis, t
    #    across, z up) and cut into cells at jambs, junction faces and every
    #    sill, head and wall height of the storey. Cells inside an opening are
    #    skipped; every other cell contributes its six sides.
    # 5. Sides shared by two cells (neighbour cells, a mitre seen from both
    #    segments, a stem end against the through wall) cancel, so only the
    #    boundary of the union remains, and coplanar boundary pieces merge
    #    into faces with holes: an outer and an inner face per segment with a
    #    hole per window, jambs, soffits, sills, tops, bottoms, and end faces
    #    only at free ends.
    #
    # Because every face is cut from the same grid, neighbouring faces meet
    # edge to edge: the result is a closed manifold by construction.
    #
    # Curved walls (0.5): an axis segment with a bulge is an arc. It keeps the
    # same local frame, s along the axis (arc length) and t across it (signed,
    # left positive), mapped to the world through polar coordinates instead
    # of a straight line: a face at offset t is the concentric arc of radius
    # r - sigma t. Joints intersect the offset curves (line-circle,
    # circle-circle), and the cells are cut at stations (Arc.tessellate's
    # equal angles plus every jamb, junction and panel edge), so each cell is
    # a short straight facet and the emitter above stays as it is.
    class WallSolver
      Seg = Struct.new(:wall, :wall_index, :index, :a, :b, :u, :n, :len, :s_base, :off_l, :off_r,
                       :height, :start_cut, :end_cut, :junctions, :splits, :pieces, :openings,
                       :arc, :stations, keyword_init: true) do
        def id = wall['id']
        def off(side) = side == :l ? off_l : off_r
        def label = wall['axis'].size > 2 || wall['closed'] ? "wall #{id} segment #{index}" : "wall #{id}"
        def arc? = !arc.nil?

        # World point at local (s, t).
        def world(s, t)
          return [a[0] + (u[0] * s) + (n[0] * t), a[1] + (u[1] * s) + (n[1] * t)] unless arc

          ang = arc[:a0] + (arc[:sigma] * s / arc[:r])
          rho = arc[:r] - (arc[:sigma] * t)
          [arc[:c][0] + (rho * Math.cos(ang)), arc[:c][1] + (rho * Math.sin(ang))]
        end

        # Local (s, t) of a world point. On an arc, s is measured on the
        # circle and wrapped around the middle of the arc, so a point a little
        # past either end gets a small negative s or one a little over len.
        def local(p)
          rel = Geometry.sub(p, a)
          return [Geometry.dot(rel, u), Geometry.dot(rel, n)] unless arc

          f = arc
          d = (Math.atan2(p[1] - f[:c][1], p[0] - f[:c][0]) - f[:a0]) * f[:sigma]
          mid = f[:sweep] / 2.0
          d -= 2.0 * Math::PI * ((d - mid + Math::PI) / (2.0 * Math::PI)).floor
          [d * f[:r], f[:sigma] * (f[:r] - Geometry.dist(p, f[:c]))]
        end

        def tangent_at(s)
          return u unless arc

          ang = arc[:a0] + (arc[:sigma] * s / arc[:r])
          [-Math.sin(ang) * arc[:sigma], Math.cos(ang) * arc[:sigma]]
        end

        def normal_at(s) = arc ? Geometry.left_normal(tangent_at(s)) : n

        # Radius of the face at offset t (arcs only).
        def face_radius(t) = arc[:r] - (arc[:sigma] * t)
      end

      End = Struct.new(:seg, :at) do
        def point = at == :start ? seg.a : seg.b
        def s = at == :start ? 0.0 : seg.len
        def dir = at == :start ? seg.tangent_at(0.0) : Geometry.scale(seg.tangent_at(seg.len), -1.0)
        def normal = seg.normal_at(s)
        def left_side = at == :start ? :l : :r
        def right_side = at == :start ? :r : :l
      end

      Piece = Struct.new(:seg, :index, :start, :finish, :openings, keyword_init: true) do
        def clear = [[start[:l], start[:r]].max, [finish[:l], finish[:r]].min]
        def span = [[start[:l], start[:r]].min, [finish[:l], finish[:r]].max]
      end

      attr_reader :segs, :warnings

      def initialize(walls, openings, storey_height: CONFIG[:storey_height_mm], config: CONFIG)
        @walls = walls
        @openings = openings
        @storey_height = storey_height.to_f
        @cfg = config
        @tol = config[:junction_tolerance_mm]
        @min_angle = config[:min_junction_angle_deg]
        @warnings = []
      end

      # Returns the layout hash: faces to build, opening frames, probe solids.
      def solve
        build_segments
        mitre_polylines
        join_free_ends
        cross_walls
        build_pieces
        check_junctions
        map_openings
        check_overlaps
        faces = build_faces
        {
          faces: faces,
          openings: opening_frames,
          solids: solids,
          z_cuts: @z_cuts,
          exterior: exterior_outline,
          exteriors: exterior_outlines,
          walls: @walls.map { |w| w['id'] },
          warnings: @warnings
        }
      end

      # --- 1. segments ------------------------------------------------------------

      def build_segments
        @segs = []
        @by_wall = {}
        @walls.each_with_index do |w, wi|
          pts = w['axis']
          count = w['closed'] ? pts.size : pts.size - 1
          off_l, off_r = offsets(w)
          base = 0.0
          list = (0...count).map do |k|
            a = pts[k][0, 2]
            b = pts[(k + 1) % pts.size][0, 2]
            d = Geometry.sub(b, a)
            arc = Arc.straight?(Arc.bulge_of(pts[k])) ? nil : Arc.frame(a, b, pts[k][2])
            len = arc ? arc[:len] : Geometry.norm(d)
            u = Geometry.scale(d, 1.0 / Geometry.norm(d))
            seg = Seg.new(wall: w, wall_index: wi, index: k, a: a, b: b, u: u, n: Geometry.left_normal(u),
                          len: len, s_base: base, off_l: off_l, off_r: off_r,
                          height: (w['height'] || @storey_height).to_f,
                          start_cut: { l: 0.0, r: 0.0, joint: false, kind: :free },
                          end_cut: { l: len, r: len, joint: false, kind: :free },
                          junctions: [], splits: [], pieces: [], openings: [], arc: arc, stations: nil)
            check_arc!(seg) if arc
            base += len
            seg
          end
          @by_wall[w['id']] = list
          @segs.concat(list)
        end
      end

      # An arc's inner face must keep wall_min_inner_radius_mm; its nominal
      # stations divide it at equal angles fine enough for the outer face.
      def check_arc!(seg)
        radii = [seg.face_radius(seg.off_l), seg.face_radius(seg.off_r)]
        min = @cfg[:wall_min_inner_radius_mm]
        if radii.min < min
          raise InvalidParams, "#{seg.label} curves too tightly: its inner face has a radius of " \
                               "#{Geometry.fmt(radii.min.round(1))} mm (axis radius #{Geometry.fmt(seg.arc[:r].round(1))} mm, " \
                               "thickness #{Geometry.fmt(seg.off_l - seg.off_r)} mm); the minimum is #{Geometry.fmt(min)} mm"
        end

        n = Arc.facets(radii.max, seg.arc[:sweep], nil, @cfg)
        seg.stations = (0..n).map { |k| seg.len * k / n }
      end

      def offsets(w)
        t = w['thickness'].to_f
        case w['justification']
        when 'left' then [t, 0.0]
        when 'right' then [0.0, -t]
        else [t / 2.0, -t / 2.0]
        end
      end

      # --- 2. joints --------------------------------------------------------------

      def mitre_polylines
        @walls.each do |w|
          list = @by_wall[w['id']]
          pairs = list.each_cons(2).to_a
          pairs << [list[-1], list[0]] if w['closed']
          pairs.each { |s_in, s_out| join!(End.new(s_in, :end), End.new(s_out, :start), "wall #{w['id']}") }
        end
      end

      # Mitres two ends that meet at one point: the face lines on the same side
      # of the corner are intersected (left of one arm with right of the other).
      def join!(e1, e2, who)
        d1 = e1.dir
        d2 = e2.dir
        angle = Math.acos(Geometry.dot(d1, d2).clamp(-1.0, 1.0)) * 180.0 / Math::PI
        if angle < @min_angle
          raise InvalidParams, "#{who}: the junction at #{Geometry.fmt_pt(e1.point)} is #{Geometry.fmt(angle)} degrees; " \
                               "the minimum is #{Geometry.fmt(@min_angle)}"
        end

        if (180.0 - angle) < 1e-7
          straight_join!(e1, e2, who)
          return
        end

        i1 = intersect(e1.seg, e1.left_side, e2.seg, e2.right_side, e1.point)
        i2 = intersect(e1.seg, e1.right_side, e2.seg, e2.left_side, e1.point)
        unless i1 && i2
          raise InvalidParams, "#{who}: the faces of the two runs never meet near #{Geometry.fmt_pt(e1.point)}; " \
                               'make the curve and the straight run cross at an angle or continue tangent'
        end

        set_cut(e1, { e1.left_side => i1[0], e1.right_side => i2[0] }, :mitre)
        set_cut(e2, { e2.right_side => i1[1], e2.left_side => i2[1] }, :mitre)
        cut_of(e1)[:partner] = e2
        cut_of(e2)[:partner] = e1
      end

      def cut_of(e) = e.at == :start ? e.seg.start_cut : e.seg.end_cut

      def straight_join!(e1, e2, who)
        p = e1.point
        a1 = Geometry.add(p, Geometry.scale(e1.normal, e1.seg.off(e1.left_side)))
        b2 = Geometry.add(p, Geometry.scale(e2.normal, e2.seg.off(e2.right_side)))
        a2 = Geometry.add(p, Geometry.scale(e1.normal, e1.seg.off(e1.right_side)))
        b1 = Geometry.add(p, Geometry.scale(e2.normal, e2.seg.off(e2.left_side)))
        if Geometry.dist(a1, b2) > @cfg[:vertex_weld_mm] * 10 || Geometry.dist(a2, b1) > @cfg[:vertex_weld_mm] * 10
          raise InvalidParams, "#{who}: two runs continue in line at #{Geometry.fmt_pt(p)} with different thickness " \
                               'or justification; give them the same faces or join them at an angle'
        end

        s1 = e1.at == :start ? 0.0 : e1.seg.len
        s2 = e2.at == :start ? 0.0 : e2.seg.len
        set_cut(e1, { l: s1, r: s1 }, :mitre)
        set_cut(e2, { l: s2, r: s2 }, :mitre)
      end

      def set_cut(e, values, kind, against = nil)
        cut = { l: values[:l], r: values[:r], joint: true, kind: kind }
        cut[:against] = against if against
        if e.at == :start
          e.seg.start_cut = cut
        else
          e.seg.end_cut = cut
        end
      end

      # Intersection of seg1's +side1+ face line with seg2's +side2+ face line:
      # [s along seg1, s along seg2], or nil when parallel. When a face is an
      # arc the curves meet up to twice; the point closest to +near+ (the
      # joint's axis point) is the one the joint means.
      def intersect(seg1, side1, seg2, side2, near = nil)
        return curve_intersect(seg1, side1, seg2, side2, near || seg1.a) if seg1.arc? || seg2.arc?

        den = Geometry.cross(seg1.u, seg2.u)
        return nil if den.abs <= 1e-12

        p1 = Geometry.add(seg1.a, Geometry.scale(seg1.n, seg1.off(side1)))
        p2 = Geometry.add(seg2.a, Geometry.scale(seg2.n, seg2.off(side2)))
        w = Geometry.sub(p2, p1)
        [Geometry.cross(w, seg2.u) / den, Geometry.cross(w, seg1.u) / den]
      end

      def curve_intersect(seg1, side1, seg2, side2, near)
        hits = face_hits(seg1, seg1.off(side1), seg2, seg2.off(side2))
        best = hits.min_by { |p| Geometry.dist(p, near) }
        best && [seg1.local(best)[0], seg2.local(best)[0]]
      end

      # World points where the face of +s1+ at offset +t1+ meets the face of
      # +s2+ at offset +t2+ (each a line or a circle).
      def face_hits(s1, t1, s2, t2)
        if s1.arc? && s2.arc?
          Arc.circle_circle(s1.arc[:c], s1.face_radius(t1), s2.arc[:c], s2.face_radius(t2))
        else
          line, lt, circle, ct = s1.arc? ? [s2, t2, s1, t1] : [s1, t1, s2, t2]
          Arc.line_circle(Geometry.add(line.a, Geometry.scale(line.n, lt)), line.u, circle.arc[:c], circle.face_radius(ct))
        end
      end

      def free_ends
        ends = []
        @walls.each do |w|
          next if w['closed']

          list = @by_wall[w['id']]
          ends << End.new(list.first, :start)
          ends << End.new(list.last, :end)
        end
        ends
      end

      def join_free_ends
        @tee_pairs = {}
        ends = free_ends
        used = {}
        ends.each_with_index do |e, i|
          next if used[i]

          mates = ends.each_index.select do |j|
            j != i && !used[j] && Geometry.dist(ends[j].point, e.point) <= @tol
          end
          next if mates.empty?

          if mates.size > 1
            ids = ([e] + mates.map { |j| ends[j] }).map { |x| x.seg.id }.uniq
            raise InvalidParams, "walls #{ids.join(', ')} all end at #{Geometry.fmt_pt(e.point)}; Plomada joins two walls " \
                                 'at a corner, so end the others on a wall axis (a T junction)'
          end
          other = ends[mates.first]
          if other.seg.wall.equal?(e.seg.wall)
            raise InvalidParams, "wall #{e.seg.id} starts where it ends at #{Geometry.fmt_pt(e.point)}; set closed: true " \
                                 'and drop the repeated point'
          end

          join!(e, other, "walls #{e.seg.id} and #{other.seg.id}")
          used[i] = used[mates.first] = true
        end
        ends.each_with_index do |e, i|
          next if used[i]

          host = tee_host(e)
          tee!(e, host) if host
        end
      end

      # The segment whose footprint band an end lies on, other than its own.
      def tee_host(e)
        p = e.point
        hits = []
        @segs.each do |v|
          next if v.equal?(e.seg)

          s, t = v.local(p)
          next if s < -@tol || s > v.len + @tol
          next if t < v.off_r - @tol || t > v.off_l + @tol

          hits << [v, s, t]
        end
        return nil if hits.empty?

        # At a mitred corner of a polyline the end lies on two arms; butt into
        # the arm the stem actually crosses, never the one it runs in line with.
        min_sin = Math.sin(@min_angle * Math::PI / 180.0)
        crossing = hits.select { |v, s, _| Geometry.cross(e.dir, v.tangent_at(s)).abs >= min_sin }
        hits = crossing unless crossing.empty?
        hit = hits.min_by { |v, s, t| [at_vertex?(v, s) ? 1 : 0, t.abs] }
        v, s, = hit
        if at_vertex?(v, s) && !mitred_vertex?(v, s)
          raise InvalidParams, "wall #{e.seg.id} ends at #{Geometry.fmt_pt(p)}, on the corner or end of wall #{v.id}; " \
                               'end it on a straight run of that wall, or at its endpoint for an L corner'
        end
        v
      end

      def at_vertex?(v, s) = s <= @tol || s >= v.len - @tol

      # A vertex joined to another run (a mitred corner) has a face that runs
      # past the axis point, so a stem can butt into it; a free end has none.
      def mitred_vertex?(v, s)
        cut = s <= @tol ? v.start_cut : v.end_cut
        cut[:joint] && cut[:kind] == :mitre
      end

      def tee!(e, v)
        d = e.dir
        at = v.local(e.point)[0]
        sin_angle = Geometry.cross(d, v.tangent_at(at)).abs
        angle = Math.asin(sin_angle.clamp(0.0, 1.0)) * 180.0 / Math::PI
        if angle < @min_angle
          raise InvalidParams, "wall #{e.seg.id} meets wall #{v.id} at #{Geometry.fmt_pt(e.point)} at " \
                               "#{Geometry.fmt(angle)} degrees; the minimum is #{Geometry.fmt(@min_angle)}"
        end

        side = Geometry.dot(d, v.normal_at(at)).positive? ? :l : :r
        il = intersect(e.seg, :l, v, side, e.point)
        ir = intersect(e.seg, :r, v, side, e.point)
        unless il && ir
          raise InvalidParams, "wall #{e.seg.id} ends at #{Geometry.fmt_pt(e.point)} inside wall #{v.id} but its faces "                                "never reach the #{side == :l ? 'left' : 'right'} face of #{v.label}"
        end

        set_cut(e, { l: il[0], r: ir[0] }, :tee, v.id)
        v.junctions << { side: side, s0: [il[1], ir[1]].min, s1: [il[1], ir[1]].max,
                         wall: e.seg.id, height: e.seg.height }
        @tee_pairs[[e.seg.object_id, v.object_id]] = true
      end

      # --- X crossings --------------------------------------------------------------

      def cross_walls
        @segs.combination(2).each do |s1, s2|
          next if s1.wall.equal?(s2.wall) && adjacent?(s1, s2)
          next if @tee_pairs[[s1.object_id, s2.object_id]] || @tee_pairs[[s2.object_id, s1.object_id]]
          next curved_crossing!(s1, s2) if s1.arc? || s2.arc?

          den = Geometry.cross(s1.u, s2.u)
          next if den.abs <= 1e-12

          w = Geometry.sub(s2.a, s1.a)
          t1 = Geometry.cross(w, s2.u) / den
          t2 = Geometry.cross(w, s1.u) / den
          next unless t1 > @tol && t1 < s1.len - @tol && t2 > @tol && t2 < s2.len - @tol

          if s1.wall.equal?(s2.wall)
            raise InvalidParams, "wall #{s1.id} crosses itself at #{Geometry.fmt_pt(Geometry.add(s1.a, Geometry.scale(s1.u, t1)))}"
          end

          through, cut, t_cut = pick_through(s1, s2, t1, t2)
          split!(cut, through, t_cut)
        end
      end

      # Plomada splits straight walls that cross in an X; a crossing where one
      # of them curves is refused instead.
      def curved_crossing!(s1, s2)
        face_hits(s1, 0.0, s2, 0.0).each do |p|
          a = s1.local(p)[0]
          b = s2.local(p)[0]
          next unless a > @tol && a < s1.len - @tol && b > @tol && b < s2.len - @tol

          who = s1.wall.equal?(s2.wall) ? "wall #{s1.id} crosses itself" : "walls #{s1.id} and #{s2.id} cross"
          raise InvalidParams, "#{who} at #{Geometry.fmt_pt(p)} where one of them curves; end one wall on the other "                                'or split them at the crossing'
        end
        nil
      end

      def adjacent?(s1, s2)
        n = @by_wall[s1.id].size
        d = (s1.index - s2.index).abs
        d == 1 || (s1.wall['closed'] && d == n - 1)
      end

      def pick_through(s1, s2, t1, t2)
        t_1 = s1.off_l - s1.off_r
        t_2 = s2.off_l - s2.off_r
        if t_1 > t_2 + 1e-9 || ((t_1 - t_2).abs <= 1e-9 && s1.wall_index <= s2.wall_index)
          [s1, s2, t2]
        else
          [s2, s1, t1]
        end
      end

      def split!(cut, through, t_cut)
        angle = Math.asin(Geometry.cross(cut.u, through.u).abs.clamp(0.0, 1.0)) * 180.0 / Math::PI
        point = Geometry.add(cut.a, Geometry.scale(cut.u, t_cut))
        if angle < @min_angle
          raise InvalidParams, "walls #{cut.id} and #{through.id} cross at #{Geometry.fmt_pt(point)} at " \
                               "#{Geometry.fmt(angle)} degrees; the minimum is #{Geometry.fmt(@min_angle)}"
        end

        back = Geometry.scale(cut.u, -1.0)
        side_a = Geometry.dot(back, through.n).positive? ? :l : :r
        side_b = side_a == :l ? :r : :l
        ends_a = %i[l r].to_h { |sd| [sd, intersect(cut, sd, through, side_a)] }
        ends_b = %i[l r].to_h { |sd| [sd, intersect(cut, sd, through, side_b)] }
        cut.splits << {
          s: t_cut,
          finish: { l: ends_a[:l][0], r: ends_a[:r][0], joint: true, kind: :cross, against: through.id },
          start: { l: ends_b[:l][0], r: ends_b[:r][0], joint: true, kind: :cross, against: through.id }
        }
        [[side_a, ends_a], [side_b, ends_b]].each do |sd, ends|
          ts = [ends[:l][1], ends[:r][1]]
          through.junctions << { side: sd, s0: ts.min, s1: ts.max, wall: cut.id, height: cut.height }
        end
      end

      # --- pieces -------------------------------------------------------------------

      def build_pieces
        @segs.each do |seg|
          starts = [seg.start_cut]
          finishes = []
          seg.splits.sort_by { |sp| sp[:s] }.each do |sp|
            finishes << sp[:finish]
            starts << sp[:start]
          end
          finishes << seg.end_cut
          starts.each_with_index do |st, i|
            fin = finishes[i]
            if fin[:l] - st[:l] <= @cfg[:cut_merge_mm] || fin[:r] - st[:r] <= @cfg[:cut_merge_mm]
              raise InvalidParams, "#{seg.label} is too short for its joints: after mitres and trims its faces run " \
                                   "#{Geometry.fmt(st[:l])}-#{Geometry.fmt(fin[:l])} and " \
                                   "#{Geometry.fmt(st[:r])}-#{Geometry.fmt(fin[:r])} mm along the axis"
            end

            seg.pieces << Piece.new(seg: seg, index: i, start: st, finish: fin, openings: [])
          end
        end
      end

      def check_junctions
        @segs.each do |seg|
          seg.junctions.each do |j|
            line_ok = seg.pieces.any? do |p|
              j[:s0] >= p.start[j[:side]] - @tol && j[:s1] <= p.finish[j[:side]] + @tol
            end
            next if line_ok

            raise InvalidParams, "wall #{j[:wall]} meets wall #{seg.id} too close to its corner or end " \
                                 "(#{Geometry.fmt(j[:s0])}-#{Geometry.fmt(j[:s1])} mm along #{seg.label}); " \
                                 'move it onto the straight run'
          end
          seg.junctions.group_by { |j| j[:side] }.each_value do |list|
            list.sort_by { |j| j[:s0] }.each_cons(2) do |j1, j2|
              next unless j2[:s0] < j1[:s1] - @tol

              raise InvalidParams, "walls #{j1[:wall]} and #{j2[:wall]} meet wall #{seg.id} at overlapping places " \
                                   "(#{Geometry.fmt(j1[:s0])}-#{Geometry.fmt(j1[:s1])} and " \
                                   "#{Geometry.fmt(j2[:s0])}-#{Geometry.fmt(j2[:s1])} mm along #{seg.label})"
            end
          end
        end
      end

      # --- 3. openings --------------------------------------------------------------

      def map_openings
        spans = Hash.new { |h, k| h[k] = [] }
        @openings.each do |o|
          list = @by_wall[o['wall']]
          raise InvalidParams, "opening #{o['id']}: wall #{o['wall'].inspect} is not in the plan" unless list

          off = o['offset'].to_f
          wid = o['width'].to_f
          total = list.sum(&:len)
          if off + wid > total + 1e-6
            raise InvalidParams, "opening #{o['id']} runs past the end of wall #{o['wall']}: " \
                                 "#{Geometry.fmt(off)} + #{Geometry.fmt(wid)} = #{Geometry.fmt(off + wid)} " \
                                 "> axis length #{Geometry.fmt(total)}"
          end

          seg = list.find { |s| off < s.s_base + s.len - 1e-6 } || list.last
          s0 = off - seg.s_base
          s1 = s0 + wid
          if s1 > seg.len + 1e-6
            raise InvalidParams, "opening #{o['id']} spans the corner of wall #{o['wall']} at offset " \
                                 "#{Geometry.fmt(seg.s_base + seg.len)} (it runs #{Geometry.fmt(off)}-" \
                                 "#{Geometry.fmt(off + wid)}); an opening must sit inside one straight run"
          end

          z0 = o['sill'].to_f
          z1 = z0 + o['height'].to_f
          if z1 > seg.height + 1e-6
            raise InvalidParams, "opening #{o['id']}: sill #{Geometry.fmt(z0)} + height #{Geometry.fmt(o['height'])} = " \
                                 "#{Geometry.fmt(z1)} is above the height of wall #{o['wall']} (#{Geometry.fmt(seg.height)})"
          end

          piece = seg.pieces.find { |p| s0 >= p.clear[0] - 1e-6 && s1 <= p.clear[1] + 1e-6 }
          unless piece
            raise InvalidParams, "opening #{o['id']} on wall #{o['wall']} crosses into a corner joint " \
                                 "(#{Geometry.fmt(s0)}-#{Geometry.fmt(s1)} mm along #{seg.label}, whose straight run is " \
                                 "#{seg.pieces.map { |p| p.clear.map { |c| Geometry.fmt(c) }.join('-') }.join(' and ')}); " \
                                 'move it along the wall'
          end

          seg.junctions.each do |j|
            next unless s0 < j[:s1] - 1e-6 && j[:s0] < s1 - 1e-6

            raise InvalidParams, "opening #{o['id']} on wall #{o['wall']} is blocked by wall #{j[:wall]}, which meets it at " \
                                 "#{Geometry.fmt(j[:s0])}-#{Geometry.fmt(j[:s1])} mm along #{seg.label} " \
                                 "(the opening runs #{Geometry.fmt(s0)}-#{Geometry.fmt(s1)})"
          end

          spans[o['wall']].each do |(a, b, other)|
            next unless off < b - 1e-6 && a < off + wid - 1e-6

            raise InvalidParams, "openings #{other} and #{o['id']} overlap on wall #{o['wall']}: " \
                                 "#{Geometry.fmt(a)}-#{Geometry.fmt(b)} and #{Geometry.fmt(off)}-#{Geometry.fmt(off + wid)}"
          end
          spans[o['wall']] << [off, off + wid, o['id']]

          if o['head'] == 'arch' && z1 > seg.height - @cfg[:cut_merge_mm]
            raise InvalidParams, "opening #{o['id']}: its arch crown at #{Geometry.fmt(z1)} reaches the top of wall " \
                                 "#{o['wall']} (#{Geometry.fmt(seg.height)}); lower it so wall remains above the arch"
          end

          entry = { rec: o, s0: s0, s1: s1, z0: z0, z1: z1 }
          entry[:panels] = arc_panels(seg, o, s0, s1) if seg.arc? && !o['curtain']
          piece.openings << entry
          seg.openings << entry
        end
      end

      # The flat panels of an opening on an arc: equal arc lengths no wider
      # than its panel_width (opening_panel_max_mm by default), each a chord at
      # mid thickness that may bow at most opening_panel_max_sagitta_mm off the
      # curve. Returns [[s_a, s_b], ...].
      def arc_panels(seg, o, s0, s1)
        if o['head'] == 'arch'
          raise InvalidParams, "opening #{o['id']}: an arched head needs a straight wall, and #{seg.label} curves; "                                'Plomada 0.5 builds arches on straight runs only'
        end

        max = (o['panel_width'] || @cfg[:opening_panel_max_mm]).to_f
        width = s1 - s0
        count = [(width / max - 1e-9).ceil, 1].max
        step = width / count
        mid_r = seg.face_radius((seg.off_l + seg.off_r) / 2.0)
        sag = mid_r * (1.0 - Math.cos(step / seg.arc[:r] / 2.0))
        limit = @cfg[:opening_panel_max_sagitta_mm]
        if sag > limit + 1e-6
          fit = (2.0 * Math.acos(1.0 - (limit / mid_r)) * seg.arc[:r]).floor
          raise InvalidParams, "opening #{o['id']} on #{seg.label}: a #{Geometry.fmt(step.round(1))} mm panel bows "                                "#{Geometry.fmt(sag.round(1))} mm off its chord at radius #{Geometry.fmt(mid_r.round(1))} mm; "                                "the most is #{Geometry.fmt(limit)} mm, so set panel_width to #{fit} mm or less"
        end

        (0...count).map { |k| [s0 + (step * k), k == count - 1 ? s1 : s0 + (step * (k + 1))] }
      end

      # Refuses footprints that overlap without a junction we resolved. An arc
      # piece is compared facet by facet (each facet a convex quad); two runs
      # joined on a curve are not compared, because a facet's chord and the
      # circle the other run was trimmed to overlap by up to the chord tolerance.
      def check_overlaps
        pieces = @segs.flat_map(&:pieces)
        parts = pieces.map { |p| footprint_parts(p) }
        pieces.each_index do |i|
          (i + 1...pieces.size).each do |j|
            next if pieces[i].seg.equal?(pieces[j].seg)
            next if curved_joint?(pieces[i].seg, pieces[j].seg)

            parts[i].product(parts[j]).each do |a, b|
              inter = Geometry.convex_intersection(a, b)
              next if inter.size < 3 || Geometry.signed_area(inter).abs < 1.0

              raise InvalidParams, "walls #{pieces[i].seg.id} and #{pieces[j].seg.id} overlap by " \
                                   "#{Geometry.fmt(Geometry.signed_area(inter).abs)} mm2 near " \
                                   "#{Geometry.fmt_pt(Geometry.centroid(inter))} without a junction Plomada can resolve; " \
                                   'end one wall on the axis of the other'
            end
          end
        end
      end

      def curved_joint?(s1, s2)
        return false unless s1.arc? || s2.arc?
        return true if @tee_pairs[[s1.object_id, s2.object_id]] || @tee_pairs[[s2.object_id, s1.object_id]]
        return true if s1.wall.equal?(s2.wall) && adjacent?(s1, s2)

        [s1.start_cut, s1.end_cut].any? { |c| c[:partner]&.seg.equal?(s2) }
      end

      # The footprint as convex polygons: one for a straight piece, one per
      # facet (between nominal stations) for an arc.
      def footprint_parts(p)
        return [footprint_world(p)] unless p.seg.arc?

        fp = footprint_local(p)
        piece_cuts(p).each_cons(2).filter_map do |c0, c1|
          cell = Geometry.clip_half_plane(fp) { |q| q[0] - c0 }
          cell = Geometry.clip_half_plane(cell) { |q| c1 - q[0] }
          cell.size < 3 ? nil : cell.map { |q| to_world(p.seg, q) }
        end
      end

      # --- 4 and 5. cells, cancellation, merge -------------------------------------

      def footprint_local(p)
        s = p.seg
        [[p.start[:r], s.off_r], [p.finish[:r], s.off_r], [p.finish[:l], s.off_l], [p.start[:l], s.off_l]]
      end

      def to_world(seg, st) = seg.world(st[0], st[1])

      def footprint_world(p) = footprint_local(p).map { |st| to_world(p.seg, st) }

      def merge_values(values, tol)
        out = []
        values.sort.each { |v| out << v if out.empty? || v - out[-1] > tol }
        out
      end

      def build_faces
        heights = @segs.map(&:height)
        zs = [0.0] + heights + @openings.flat_map { |o| [o['sill'].to_f, o['sill'].to_f + o['height'].to_f] }
        # An arch springs at its crown minus its rise; the cells beside it are cut there.
        zs += @openings.select { |o| o['head'] == 'arch' }.map { |o| o['sill'].to_f + o['height'].to_f - o['arch_rise'].to_f }
        @z_cuts = merge_values(zs, @cfg[:cut_merge_mm])
        @mesh = Mesh.new(@cfg[:vertex_weld_mm])
        @segs.each { |seg| seg.pieces.each { |p| emit_piece(p) } }
        @cancelled = @mesh.cancel_shared!
        @mesh.merged.map do |f|
          {
            wall: f[:meta][:wall],
            part: f[:meta][:part],
            segment: f[:meta][:seg],
            outer: f[:outer].map { |i| @mesh.pool[i] },
            holes: f[:holes].map { |h| h.map { |i| @mesh.pool[i] } },
            outer_ids: f[:outer],
            hole_ids: f[:holes],
            normal: f[:normal]
          }.tap { |h| h[:curved] = true if f[:meta][:curved] }
        end
      end

      # Two runs share the diagonal face of a mitre, and the shared faces only
      # cancel when both runs cut that diagonal at the same points. A junction
      # cut that lands inside the partner's mitre zone (a stem butting into a
      # corner) is carried across: the point where it meets the diagonal is
      # mapped onto this run's axis.
      def mitre_partner_cuts(piece)
        seg = piece.seg
        out = []
        ends = []
        ends << seg.start_cut if piece.start.equal?(seg.start_cut)
        ends << seg.end_cut if piece.finish.equal?(seg.end_cut)
        ends.each do |cut|
          partner = cut[:partner]
          next unless cut[:kind] == :mitre && partner

          pseg = partner.seg
          pcut = cut_of(partner)
          lo, hi = [pcut[:l], pcut[:r]].minmax
          next if (hi - lo) <= 1e-9

          pseg.junctions.each do |j|
            [j[:s0], j[:s1]].each do |c|
              next unless c > lo + 1e-9 && c < hi - 1e-9

              f = (c - pcut[:l]) / (pcut[:r] - pcut[:l])
              world = if pseg.arc?
                        # The mitre face is the straight line between its two face points.
                        l_pt = to_world(pseg, [pcut[:l], pseg.off_l])
                        r_pt = to_world(pseg, [pcut[:r], pseg.off_r])
                        Geometry.add(l_pt, Geometry.scale(Geometry.sub(r_pt, l_pt), f))
                      else
                        to_world(pseg, [c, pseg.off_l + ((pseg.off_r - pseg.off_l) * f)])
                      end
              out << seg.local(world)[0]
            end
          end
        end
        out
      end

      # The s values where a piece is cut into columns: its ends, every jamb,
      # panel edge, junction face and mitre-partner cut, and on an arc the
      # stations between them. Computed once per piece.
      def piece_cuts(piece)
        (@piece_cuts ||= {})[piece.object_id] ||= begin
          seg = piece.seg
          lo, hi = piece.span
          cuts = [lo, hi]
          piece.openings.each { |o| cuts.push(o[:s0], o[:s1]) }
          piece.openings.each { |o| (o[:panels] || []).each { |pa, pb| cuts.push(pa, pb) } }
          seg.junctions.each { |j| cuts.push(j[:s0], j[:s1]) }
          cuts.concat(mitre_partner_cuts(piece))
          cuts.concat(arc_stations(piece, cuts)) if seg.arc?
          cuts = merge_values(cuts.select { |c| c >= lo - 1e-9 && c <= hi + 1e-9 }, @cfg[:cut_merge_mm])
          cuts[0] = lo
          cuts[-1] = hi
          cuts
        end
      end

      def emit_piece(piece)
        seg = piece.seg
        cuts = piece_cuts(piece)
        zc = @z_cuts.select { |z| z < seg.height - @cfg[:cut_merge_mm] } + [seg.height]
        fp = footprint_local(piece)
        prefix = "#{seg.id}|#{seg.index}|#{piece.index}|"
        cols = cuts.each_cons(2).to_a
        rows = zc.each_cons(2).to_a
        # void[k][m]: column k, row m lies inside an opening. Sides shared by
        # two solid cells of this piece would cancel, so they are never emitted.
        void = cols.map { |c0, c1| rows.map { |z0, z1| void?(piece, c0, c1, z0, z1) } }
        cols.each_with_index do |(c0, c1), k|
          cell = Geometry.clip_half_plane(fp) { |q| q[0] - c0 }
          cell = Geometry.clip_half_plane(cell) { |q| c1 - q[0] }
          next if cell.size < 3 || Geometry.signed_area(cell).abs < 1e-6

          kinds = cell.each_index.map { |i| edge_kind(piece, cell[i], cell[(i + 1) % cell.size], c0, c1) }
          world = cell.map { |q| to_world(seg, q) }
          rows.each_with_index do |(z0, z1), m|
            next if void[k][m]

            open = {
              bottom: m.zero? || void[k][m - 1],
              top: m == rows.size - 1 || void[k][m + 1],
              c0: k.zero? || void[k - 1][m],
              c1: k == cols.size - 1 || void[k + 1][m]
            }
            emit_prism(world, kinds, z0, z1, seg, prefix, c0, c1, open, k)
          end
        end
        piece.openings.select { |o| arch?(o) }.each { |o| emit_arch(piece, o, zc, prefix) }
      end

      # The nominal stations of an arc piece that become cuts: those inside
      # the piece, away from its mitre zones (a station there would split the
      # diagonal face the partner run shares), outside every junction span
      # (the stem's end face must meet one host facet) and not so close to a
      # structural cut that they would leave a sliver facet.
      def arc_stations(piece, structural)
        seg = piece.seg
        lo, hi = piece.span
        facet = seg.len / (seg.stations.size - 1)
        gap = [@cfg[:cut_merge_mm], facet * 0.25].max
        zones = [[piece.start[:l], piece.start[:r]].minmax, [piece.finish[:l], piece.finish[:r]].minmax]
        zones.concat(seg.junctions.map { |j| [j[:s0], j[:s1]] })
        seg.stations.select do |c|
          c > lo + gap && c < hi - gap &&
            zones.none? { |a, b| c > a - gap && c < b + gap } &&
            structural.none? { |x| (x - c).abs < gap }
        end
      end

      # A cell is void when it lies inside an opening. Cuts closer than
      # cut_merge_mm were merged, so the comparison allows that much slack.
      # An arched opening's cells are void from its sill to the wall top: the
      # wall above its spring line is emitted whole by emit_arch.
      def void?(piece, c0, c1, z0, z1)
        tol = @cfg[:cut_merge_mm]
        piece.openings.any? do |o|
          top = arch?(o) ? piece.seg.height : o[:z1]
          c0 >= o[:s0] - tol && c1 <= o[:s1] + tol && z0 >= o[:z0] - tol && z1 <= top + tol
        end
      end

      def arch?(o) = o[:rec]['head'] == 'arch'

      # The wall above an arched opening's spring line, as one block: its two
      # faces are the rectangle up to the wall top minus the arch (tessellated
      # at the chord tolerance), the soffit is one strip per arch facet between
      # them, the sides are cut at every row so they cancel against the cells
      # beside the opening, and the top merges with the wall's top.
      def emit_arch(piece, o, zc, prefix)
        seg = piece.seg
        h = seg.height
        s0 = o[:s0]
        s1 = o[:s1]
        rise = o[:rec]['arch_rise'].to_f
        zs = o[:z1] - rise
        rows = zc.select { |z| z > zs + 1e-9 && z < h - 1e-9 }
        curve = Arc.tessellate([s0, zs], [s1, zs], -2.0 * rise / (s1 - s0), nil, @cfg)
        meta = { wall: seg.id, seg: seg.index }
        lift = ->(s, t, z) { to_world(seg, [s, t]) + [z] }
        { l: seg.off_l, r: seg.off_r }.each do |side, t|
          ring = [[s0, zs]] + rows.map { |z| [s0, z] } + [[s0, h], [s1, h]] + rows.reverse.map { |z| [s1, z] } +
                 [[s1, zs]] + curve[1..-2].reverse
          loop_pts = ring.map { |sv, z| lift.call(sv, t, z) }
          normal = side == :l ? seg.n : Geometry.scale(seg.n, -1.0)
          loop_pts = loop_pts.reverse if Geometry.dot3(Geometry.newell(loop_pts), normal + [0.0]).negative?
          name = side == :l ? 'left' : 'right'
          @mesh.add(loop_pts, "#{prefix}#{name}", meta.merge(part: name))
        end
        ([zs] + rows + [h]).each_cons(2) do |za, zb|
          [[s0, -1.0], [s1, 1.0]].each do |sv, dir|
            quad = [lift.call(sv, seg.off_l, za), lift.call(sv, seg.off_r, za), lift.call(sv, seg.off_r, zb),
                    lift.call(sv, seg.off_l, zb)]
            out = Geometry.scale(seg.u, dir) + [0.0]
            quad = quad.reverse if Geometry.dot3(Geometry.newell(quad), out).negative?
            @mesh.add(quad, "#{prefix}cut#{Geometry.fmt(sv)}#{dir.positive? ? '+' : '-'}", meta.merge(part: 'jamb'))
          end
        end
        top = [lift.call(s0, seg.off_r, h), lift.call(s1, seg.off_r, h), lift.call(s1, seg.off_l, h), lift.call(s0, seg.off_l, h)]
        top = top.reverse if Geometry.newell(top)[2].negative?
        @mesh.add(top, "#{prefix}top", meta.merge(part: 'top'))
        center_z = zs - (Arc.center_radius([s0, zs], [s1, zs], -2.0 * rise / (s1 - s0))[0][1] - zs).abs
        mid_s = (s0 + s1) / 2.0
        curve.each_cons(2).with_index do |(a, b), k|
          quad = [lift.call(a[0], seg.off_l, a[1]), lift.call(b[0], seg.off_l, b[1]), lift.call(b[0], seg.off_r, b[1]),
                  lift.call(a[0], seg.off_r, a[1])]
          ms = (a[0] + b[0]) / 2.0
          mz = (a[1] + b[1]) / 2.0
          toward = Geometry.scale(seg.u, mid_s - ms) + [center_z - mz]
          quad = quad.reverse if Geometry.dot3(Geometry.newell(quad), toward).negative?
          @mesh.add(quad, "#{prefix}arch#{k}", meta.merge(part: 'soffit'))
        end
      end

      # Which plane an edge of a cell (local s,t coordinates) lies on.
      def edge_kind(piece, p, q, c0, c1)
        seg = piece.seg
        return [:face, :r] if (p[1] - seg.off_r).abs < 1e-9 && (q[1] - seg.off_r).abs < 1e-9
        return [:face, :l] if (p[1] - seg.off_l).abs < 1e-9 && (q[1] - seg.off_l).abs < 1e-9
        return [:end, :start] if on_cut_line?(piece.start, seg, p) && on_cut_line?(piece.start, seg, q)
        return [:end, :finish] if on_cut_line?(piece.finish, seg, p) && on_cut_line?(piece.finish, seg, q)
        return [:cut, c0] if (p[0] - c0).abs < 1e-9 && (q[0] - c0).abs < 1e-9
        return [:cut, c1] if (p[0] - c1).abs < 1e-9 && (q[0] - c1).abs < 1e-9

        [:end, :unknown]
      end

      def on_cut_line?(cut, seg, pt)
        a = [cut[:r], seg.off_r]
        b = [cut[:l], seg.off_l]
        e = Geometry.sub(b, a)
        Geometry.cross(e, Geometry.sub(pt, a)).abs / Geometry.norm(e) < 1e-7
      end

      # +col+ is the column index: an arc's face is one plane per facet, so
      # its left and right sides merge per column only and carry curved: true
      # (their vertical edges are softened in SketchUp).
      def emit_prism(world, kinds, z0, z1, seg, prefix, c0, c1, open, col = 0)
        wall = seg.id
        h = seg.height
        if open[:bottom]
          @mesh.add(world.reverse.map { |q| [q[0], q[1], z0] },
                    z0.abs < 1e-9 ? "#{prefix}bottom" : "#{prefix}z#{Geometry.fmt(z0)}-",
                    { wall: wall, seg: seg.index, part: z0.abs < 1e-9 ? 'bottom' : 'soffit' })
        end
        if open[:top]
          @mesh.add(world.map { |q| [q[0], q[1], z1] },
                    (z1 - h).abs < 1e-9 ? "#{prefix}top" : "#{prefix}z#{Geometry.fmt(z1)}+",
                    { wall: wall, seg: seg.index, part: (z1 - h).abs < 1e-9 ? 'top' : 'sill' })
        end
        world.each_index do |i|
          p = world[i]
          q = world[(i + 1) % world.size]
          kind, which = kinds[i]
          next if kind == :cut && !open[(which - c0).abs < 1e-9 ? :c0 : :c1]
          meta = { wall: wall, seg: seg.index }
          key, part = case kind
                      when :face
                        side = which == :l ? 'left' : 'right'
                        meta[:curved] = true if seg.arc?
                        [seg.arc? ? "#{side}@#{col}" : side, side]
                      when :cut
                        outward = [q[1] - p[1], -(q[0] - p[0])]
                        sign = Geometry.dot(outward, seg.tangent_at(which)).positive? ? '+' : '-'
                        ["cut#{Geometry.fmt(which)}#{sign}", 'jamb']
                      when :end
                        which == :unknown ? ["edge#{@mesh.faces.size}", 'end'] : [which.to_s, 'end']
                      end
          @mesh.add([[p[0], p[1], z0], [q[0], q[1], z0], [q[0], q[1], z1], [p[0], p[1], z1]],
                    "#{prefix}#{key}", meta.merge(part: part))
        end
      end

      # --- outputs ---------------------------------------------------------------

      # Where each opening's component goes: origin at the near jamb, on the
      # wall's mid-thickness plane, at sill height; x along the axis, y across
      # (toward the axis's left side), z up.
      # On an arc, one frame per flat panel (see arc_panels): origin at the
      # panel's near edge on the mid-thickness arc, x along its chord.
      def opening_frames
        @segs.flat_map do |seg|
          seg.openings.flat_map do |o|
            next [] if o[:rec]['curtain'] # a curtain wall's void: its own builder glazes it
            next panel_frames(seg, o) if o[:panels]

            mid = (seg.off_l + seg.off_r) / 2.0
            base = to_world(seg, [o[:s0], mid])
            rec = o[:rec]
            {
              id: rec['id'], wall: seg.id, kind: rec['opening_kind'], tag: rec['tag'] || rec['id'],
              width: o[:s1] - o[:s0], height: o[:z1] - o[:z0], sill: o[:z0],
              swing: rec['swing'], hand: rec['hand'],
              thickness: seg.off_l - seg.off_r,
              origin: [base[0], base[1], o[:z0]],
              xaxis: [seg.u[0], seg.u[1], 0.0],
              yaxis: [seg.n[0], seg.n[1], 0.0],
              segment: seg.index, local: [o[:s0], o[:s1]],
              record: rec
            }
          end
        end
      end

      def panel_frames(seg, o)
        mid = (seg.off_l + seg.off_r) / 2.0
        rec = o[:rec]
        o[:panels].each_with_index.map do |(pa, pb), k|
          a = to_world(seg, [pa, mid])
          b = to_world(seg, [pb, mid])
          x = Geometry.unit(Geometry.sub(b, a))
          y = Geometry.left_normal(x)
          {
            id: rec['id'], wall: seg.id, kind: rec['opening_kind'], tag: rec['tag'] || rec['id'],
            width: Geometry.dist(a, b), height: o[:z1] - o[:z0], sill: o[:z0],
            swing: rec['swing'], hand: rec['hand'],
            thickness: seg.off_l - seg.off_r,
            origin: [a[0], a[1], o[:z0]],
            xaxis: [x[0], x[1], 0.0],
            yaxis: [y[0], y[1], 0.0],
            segment: seg.index, local: [pa, pb], panel: k, panels: o[:panels].size,
            record: rec
          }
        end
      end

      # Solids for strip_internal_faces: each piece's footprint, height and voids.
      # An arc piece gives one convex solid per emitted facet, cut exactly where
      # its faces are, so a probe 1 mm off a facet never lands inside a chord
      # of a neighbouring facet; a facet inside an opening is void in full.
      def solids
        @segs.flat_map do |seg|
          seg.pieces.flat_map do |p|
            next arc_solids(p) if seg.arc?

            [{
              wall: seg.id, poly: footprint_world(p), z0: 0.0, z1: seg.height,
              a: seg.a, u: seg.u,
              voids: p.openings.map { |o| { s0: o[:s0], s1: o[:s1], z0: o[:z0], z1: o[:z1] } }
            }]
          end
        end
      end

      def arc_solids(p)
        seg = p.seg
        fp = footprint_local(p)
        tol = @cfg[:cut_merge_mm]
        piece_cuts(p).each_cons(2).filter_map do |c0, c1|
          cell = Geometry.clip_half_plane(fp) { |q| q[0] - c0 }
          cell = Geometry.clip_half_plane(cell) { |q| c1 - q[0] }
          next if cell.size < 3

          voids = p.openings.select { |o| c0 >= o[:s0] - tol && c1 <= o[:s1] + tol }
                   .map { |o| { s0: -Float::INFINITY, s1: Float::INFINITY, z0: o[:z0], z1: o[:z1] } }
          poly = cell.map { |q| to_world(seg, q) }
          { wall: seg.id, poly: poly, z0: 0.0, z1: seg.height, a: poly[0], u: [1.0, 0.0], voids: voids }
        end
      end

      # The outer face line of the closed wall with the largest enclosed area,
      # counter-clockwise; nil when the plan has no closed wall.
      # The largest building's outline (kept for callers that need one).
      def exterior_outline = exterior_outlines.first

      # One outline per building: every closed wall that is not drawn inside
      # another closed wall, largest first. Each one gets a slab and a roof.
      def exterior_outlines
        closed = @walls.select { |w| w['closed'] }
        outer = closed.reject do |w|
          probe = w['axis'][0]
          closed.any? { |o| !o.equal?(w) && Geometry.point_in_polygon?(probe, Geometry.axis_ring(o)) }
        end
        outer.sort_by { |w| -Geometry.signed_area(Geometry.axis_ring(w)).abs }.map { |w| outline_of(w) }
      end

      # The outer face of a closed wall; along an arc it passes through the
      # same facet vertices as the wall's face.
      def outline_of(w)
        side = Geometry.signed_area(Geometry.axis_ring(w)).positive? ? :r : :l
        pts = @by_wall[w['id']].flat_map do |seg|
          first = to_world(seg, [seg.start_cut[side], seg.off(side)])
          next [first] unless seg.arc?

          lo = seg.start_cut[side]
          hi = seg.end_cut[side]
          inner = seg.pieces.flat_map { |p| piece_cuts(p) }.uniq.select { |c| c > lo + 1e-6 && c < hi - 1e-6 }.sort
          [first] + inner.map { |c| to_world(seg, [c, seg.off(side)]) }
        end
        pts = pts.reverse if Geometry.signed_area(pts).negative?
        { wall: w['id'], points: pts, thickness: w['thickness'].to_f }
      end

      def stats
        { cancelled: @cancelled, z_cuts: @z_cuts }
      end
    end

    module_function

    # A closed wall's axis as a plain ring, arcs tessellated (the axis itself
    # when it has none).
    def axis_ring(wall) = Arc.curved?(wall['axis']) ? Arc.polyline(wall['axis'], closed: true) : wall['axis']

    # Solves walls and openings; see WallSolver. Each building (walls that
    # touch, see wall_components) is solved on its own and the layouts merged:
    # a building's sill and head heights then only cut its own walls.
    def solve_walls(walls, openings, storey_height: CONFIG[:storey_height_mm], config: CONFIG)
      merge_layouts(solve_buildings(walls, openings, storey_height: storey_height, config: config))
    end

    # One layout per building, largest plan first is not implied: the order is
    # the order of each building's first wall in +walls+.
    def solve_buildings(walls, openings, storey_height: CONFIG[:storey_height_mm], config: CONFIG)
      building_plans(walls, openings, config).map do |ws, os|
        WallSolver.new(ws, os, storey_height: storey_height, config: config).solve
      end
    end

    # [[walls, openings]] per building.
    def building_plans(walls, openings, config = CONFIG)
      wall_components(walls, config).map do |idx|
        ws = idx.map { |i| walls[i] }
        ids = ws.to_h { |w| [w['id'], true] }
        [ws, openings.select { |o| ids[o['wall']] }]
      end
    end

    # Groups of wall indices whose bodies may touch: union-find over plan
    # boxes grown by each wall's thickness plus the junction tolerance (a box
    # test, so two walls that only come close share a group; that is safe).
    def wall_components(walls, config = CONFIG)
      grow = config[:junction_tolerance_mm] + 1.0
      boxes = walls.map do |w|
        pts = Arc.curved?(w['axis']) ? Arc.polyline(w['axis'], closed: w['closed']) : w['axis']
        xs = pts.map { |q| q[0] }
        ys = pts.map { |q| q[1] }
        g = w['thickness'].to_f + grow
        [xs.min - g, ys.min - g, xs.max + g, ys.max + g]
      end
      parent = (0...walls.size).to_a
      find = lambda do |i|
        i = parent[i] = parent[parent[i]] while parent[i] != i
        i
      end
      boxes.each_with_index do |a, i|
        (0...i).each do |j|
          b = boxes[j]
          next if a[0] > b[2] || b[0] > a[2] || a[1] > b[3] || b[1] > a[3]

          ri = find.call(i)
          rj = find.call(j)
          parent[[ri, rj].max] = [ri, rj].min
        end
      end
      (0...walls.size).group_by { |i| find.call(i) }.values
    end

    # One layout from several buildings' layouts. Vertex ids are renumbered
    # so each building keeps its own; exteriors are sorted largest first.
    def merge_layouts(layouts)
      return layouts.first if layouts.size == 1

      offset = 0
      faces = layouts.flat_map do |lay|
        ids = lay[:faces].flat_map { |f| f[:outer_ids] + f[:hole_ids].flatten }
        shifted = lay[:faces].map do |f|
          f.merge(outer_ids: f[:outer_ids].map { |i| i + offset },
                  hole_ids: f[:hole_ids].map { |h| h.map { |i| i + offset } })
        end
        offset += (ids.max || -1) + 1
        shifted
      end
      exteriors = layouts.flat_map { |l| l[:exteriors] || [] }.sort_by { |o| -signed_area(o[:points]).abs }
      {
        faces: faces,
        openings: layouts.flat_map { |l| l[:openings] },
        solids: layouts.flat_map { |l| l[:solids] },
        z_cuts: layouts.flat_map { |l| l[:z_cuts] }.uniq.sort,
        exterior: exteriors.first,
        exteriors: exteriors,
        walls: layouts.flat_map { |l| l[:walls] },
        warnings: layouts.flat_map { |l| l[:warnings] }
      }
    end
  end
end
