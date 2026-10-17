# frozen_string_literal: true

require_relative 'core'
require_relative 'arc'
require_relative '../config'

module Plomada
  module Geometry
    module_function

    # The six faces of an axis-aligned box, each counter-clockwise seen from outside.
    def box_faces(min, max)
      x0, y0, z0 = min
      x1, y1, z1 = max
      [
        [[x0, y0, z0], [x0, y1, z0], [x1, y1, z0], [x1, y0, z0]], # bottom, -z
        [[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], # top, +z
        [[x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1]], # -y
        [[x1, y1, z0], [x0, y1, z0], [x0, y1, z1], [x1, y1, z1]], # +y
        [[x0, y1, z0], [x0, y0, z0], [x0, y0, z1], [x0, y1, z1]], # -x
        [[x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]]  # +x
      ]
    end

    # A vertical prism over a counter-clockwise polygon, from z0 to z1.
    def prism_faces(poly, z0, z1)
      faces = [poly.reverse.map { |p| [p[0], p[1], z0] }, poly.map { |p| [p[0], p[1], z1] }]
      poly.each_index do |i|
        p = poly[i]
        q = poly[(i + 1) % poly.size]
        faces << [[p[0], p[1], z0], [q[0], q[1], z0], [q[0], q[1], z1], [p[0], p[1], z1]]
      end
      faces
    end

    # An extrusion of a profile drawn in a vertical plane: +profile+ is a list
    # of [w, z] pairs, counter-clockwise when seen looking along -+axis_l+ from
    # +origin+ + l1 * axis_l; +axis_w+ maps w to plan, +axis_l+ is the
    # extrusion direction (axis_w x axis_l must point down, so w, z, l is right-handed).
    def extrude_profile(profile, origin, axis_w, axis_l, l0, l1)
      at = lambda do |w, z, l|
        [origin[0] + (axis_w[0] * w) + (axis_l[0] * l), origin[1] + (axis_w[1] * w) + (axis_l[1] * l), z]
      end
      front = profile.map { |w, z| at.call(w, z, l1) }
      back = profile.reverse.map { |w, z| at.call(w, z, l0) }
      faces = [front, back]
      profile.each_index do |i|
        w0, z0 = profile[i]
        w1, z1 = profile[(i + 1) % profile.size]
        faces << [at.call(w0, z0, l0), at.call(w1, z1, l0), at.call(w1, z1, l1), at.call(w0, z0, l1)]
      end
      faces
    end

    # Parts of a window component in its own frame (x along the wall from the
    # near jamb, y across with 0 on the wall's mid plane, z up from the sill):
    # a frame ring of four boxes and one glass pane at mid-thickness.
    def window_parts(width, height, config = CONFIG)
      f = config[:window_frame_mm]
      g = config[:glass_thickness_mm] / 2.0
      d = f / 2.0
      if width <= 2 * f + 1 || height <= 2 * f + 1
        return [{ name: 'vidrio', material: :glass, min: [0.0, -g, 0.0], max: [width, g, height] }]
      end

      [
        { name: 'marco_izq', material: :frame, min: [0.0, -d, 0.0], max: [f, d, height] },
        { name: 'marco_der', material: :frame, min: [width - f, -d, 0.0], max: [width, d, height] },
        { name: 'marco_inf', material: :frame, min: [f, -d, 0.0], max: [width - f, d, f] },
        { name: 'marco_sup', material: :frame, min: [f, -d, height - f], max: [width - f, d, height] },
        { name: 'vidrio', material: :glass, min: [f, -g, f], max: [width - f, g, height - f] }
      ]
    end

    # Parts of a door component in the same frame: a frame on three sides
    # flush with the face the door swings toward, and a leaf hinged per the
    # AutoCAD MCP Pro convention (swing "in" opens to the left of the wall
    # axis; hand names the hinge jamb seen from the swing side), drawn closed.
    def door_parts(width, height, thickness, swing, hand, config = CONFIG)
      face = config[:door_frame_face_mm]
      depth = [config[:door_frame_depth_mm], thickness].min
      leaf = [config[:door_leaf_mm], depth].min
      side = swing == 'in' ? 1.0 : -1.0
      outer_y = side * thickness / 2.0
      frame_y = [outer_y, outer_y - (side * depth)].minmax
      leaf_y = [outer_y, outer_y - (side * leaf)].minmax
      toward = hand == 'left' ? side : -side
      hinge_x = toward.positive? ? width - face : face
      under = config[:door_undercut_mm]
      [
        { name: 'marco_izq', material: :wood, min: [0.0, frame_y[0], 0.0], max: [face, frame_y[1], height] },
        { name: 'marco_der', material: :wood, min: [width - face, frame_y[0], 0.0], max: [width, frame_y[1], height] },
        { name: 'marco_sup', material: :wood, min: [face, frame_y[0], height - face], max: [width - face, frame_y[1], height] },
        { name: 'hoja', material: :wood, min: [face, leaf_y[0], under], max: [width - face, leaf_y[1], height - face],
          pivot: [hinge_x, outer_y, 0.0], hinge: toward.positive? ? 'far_jamb' : 'near_jamb' }
      ]
    end

    # --- arched openings ----------------------------------------------------------------

    # The arch of an opening +width+ wide springing at +spring+ with +rise+, in
    # the component's (x, z) plane: {curve: outer points from x 0 to width,
    # center:, radius:}.
    def arch_curve(width, spring, rise, config = CONFIG)
      bulge = -2.0 * rise / width
      center, radius = Arc.center_radius([0.0, spring], [width, spring], bulge)
      { curve: Arc.tessellate([0.0, spring], [width, spring], bulge, nil, config), center: center, radius: radius }
    end

    # Points of the arch's concentric inner curve +inset+ inside it, from right
    # to left, clipped at the spring line (walked right to left, the curve
    # bulging up is counter-clockwise: a positive bulge).
    def arch_inner(arch, spring, inset, config = CONFIG)
      c = arch[:center]
      r = arch[:radius] - inset
      half = Math.sqrt([(r * r) - ((spring - c[1])**2), 0.0].max)
      a = [c[0] + half, spring]
      b = [c[0] - half, spring]
      sagitta = c[1] + r - spring
      Arc.tessellate(a, b, 2.0 * sagitta / dist(a, b), nil, config)
    end

    # Extrudes an (x, z) profile across the wall from y0 to y1 (closed solid).
    def profile_faces(profile, y0, y1)
      prof = dedupe_ring(profile)
      prof = prof.reverse if signed_area(prof).negative?
      extrude_profile(prof, [0.0, 0.0], [1.0, 0.0], [0.0, -1.0], -y1, -y0)
    end

    # An arched window: jambs and sill up to the spring line, a curved head
    # member, one glass pane filling the rectangle and the arch.
    def arch_window_parts(width, height, rise, config = CONFIG)
      f = config[:window_frame_mm]
      g = config[:glass_thickness_mm] / 2.0
      d = f / 2.0
      spring = height - rise
      arch = arch_curve(width, spring, rise, config)
      inner = arch_inner(arch, spring, f, config)
      [
        { name: 'marco_izq', material: :frame, min: [0.0, -d, 0.0], max: [f, d, spring] },
        { name: 'marco_der', material: :frame, min: [width - f, -d, 0.0], max: [width, d, spring] },
        { name: 'marco_inf', material: :frame, min: [f, -d, 0.0], max: [width - f, d, f] },
        { name: 'marco_arco', material: :frame, faces: profile_faces(arch[:curve] + inner, -d, d) },
        { name: 'vidrio', material: :glass,
          faces: profile_faces([[f, f], [width - f, f], [width - f, spring]] + inner + [[f, spring]], -g, g) }
      ]
    end

    # An arched door: the door of door_parts up to the spring line and a
    # glazed fanlight in the arch above it.
    def arch_door_parts(width, height, rise, thickness, swing, hand, config = CONFIG)
      f = config[:window_frame_mm]
      g = config[:glass_thickness_mm] / 2.0
      d = f / 2.0
      spring = height - rise
      arch = arch_curve(width, spring, rise, config)
      inner = arch_inner(arch, spring, f, config)
      door_parts(width, spring, thickness, swing, hand, config) + [
        { name: 'marco_arco', material: :frame, faces: profile_faces(arch[:curve] + inner, -d, d) },
        { name: 'vidrio', material: :glass, faces: profile_faces(inner + [[f, spring]], -g, g) }
      ]
    end

    # Flat roof outline: the outer face offset outward by the overhang.
    def flat_roof_outline(outline, overhang)
      overhang.positive? ? offset_polygon(outline, overhang) : remove_collinear(outline)
    end

    # A gable roof over a rectangular outline: two sheets meeting at a ridge
    # along the longer side, cut vertically at the eaves, plus a triangular
    # gable wall on each short side. Lengths in mm, pitch in degrees.
    def gable_roof(outline, wall_top, overhang, pitch_deg, thickness, wall_thickness)
      pts = remove_collinear(outline)
      raise InvalidParams, 'gable needs a rectangular footprint; use flat' unless rectangle?(pts)

      e0 = sub(pts[1], pts[0])
      e1 = sub(pts[2], pts[1])
      origin, axis_l, len_l, axis_w, len_w =
        if norm(e0) >= norm(e1)
          [pts[0], unit(e0), norm(e0), unit(e1), norm(e1)]
        else
          [pts[1], unit(e1), norm(e1), unit(scale(e0, -1.0)), norm(e0)]
        end
      # Keep (w, z, l) right-handed: axis_w x axis_l must point down.
      if cross(axis_w, axis_l).positive?
        origin = add(origin, scale(axis_w, len_w))
        axis_w = scale(axis_w, -1.0)
      end
      tan = Math.tan(pitch_deg * Math::PI / 180.0)
      vertical = thickness / Math.cos(pitch_deg * Math::PI / 180.0)
      half = len_w / 2.0
      eave = wall_top - (overhang * tan)
      ridge = wall_top + (half * tan)
      sheet = [
        [-overhang, eave], [half, ridge], [len_w + overhang, eave],
        [len_w + overhang, eave + vertical], [half, ridge + vertical], [-overhang, eave + vertical]
      ]
      roof = extrude_profile(sheet, origin, axis_w, axis_l, -overhang, len_l + overhang)
      gable = [[0.0, wall_top], [len_w, wall_top], [half, ridge]]
      gables = [
        extrude_profile(gable, origin, axis_w, axis_l, 0.0, wall_thickness),
        extrude_profile(gable, origin, axis_w, axis_l, len_l - wall_thickness, len_l)
      ]
      { roof: roof, gables: gables, ridge_z: ridge + vertical, axis: axis_l }
    end

    # Text and position of a room label.
    def room_label(room, config = CONFIG)
      area = format('%.2f', room['area'].to_f / 1_000_000.0)
      head = room['number'] ? "#{room['number']} #{room['name']}" : room['name']
      { text: "#{head}\n#{area} m²", at: [room['at'][0], room['at'][1], config[:room_label_z_mm]] }
    end
  end
end
