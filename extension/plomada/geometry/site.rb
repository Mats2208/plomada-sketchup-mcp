# frozen_string_literal: true

require_relative 'core'
require_relative 'parts'
require_relative 'planters'
require_relative 'stairs'

module Plomada
  # The site around the house and the fitting of library components, as
  # plain face loops in mm. Ground level is the top of the terrain (the
  # underside of the ground-floor slab); every site part sits on it.
  module Geometry
    module_function

    PAVING_TOP_MM = 20.0        # mm below the ground floor: a paving top sits just under the floor (z 0)
    FENCE_THICKNESS_MM = 40.0   # mm, fence board
    POOL_COPING_MM = 300.0      # mm, width of the coping around a pool
    POOL_WALL_MM = 200.0        # mm, pool wall thickness
    POOL_FLOOR_MM = 200.0       # mm, pool floor thickness
    POOL_WATER_DROP_MM = 250.0  # mm, water surface below the coping top

    def ccw(points)
      pts = remove_collinear(points.map { |p| [p[0].to_f, p[1].to_f] })
      raise InvalidParams, 'a site polygon needs at least 3 distinct corners' if pts.size < 3

      signed_area(pts).negative? ? pts.reverse : pts
    end

    # Paving or a deck: a slab from the ground up to just under the floor.
    def paving_faces(points, ground, top = -PAVING_TOP_MM)
      raise InvalidParams, "paving top #{fmt_mm(top)} must be above the ground #{fmt_mm(ground)}" unless top > ground

      prism_faces(ccw(points), ground, top)
    end

    # The library keys whose site objects stand in as massing so that the
    # site's library models stay within +budget+ faces. +counts+ and +faces+
    # are {key => objects placed} and {key => faces of one model}; the
    # heaviest contribution (faces x objects) goes first, ties by key.
    #
    # A site of 32 hedges (537,600 faces each), 104 ficus (66,586) and 44
    # sedans (111,922) is about 29 M faces, and SketchUp 2025 crashed
    # twice right after building it.
    def site_proxy_keys(counts, faces, budget)
      weight = ->(k, n) { n * faces.fetch(k, 0) }
      total = counts.sum { |k, n| weight.call(k, n) }
      keys = []
      counts.sort_by { |k, n| [-weight.call(k, n), k.to_s] }.each do |k, n|
        break if total <= budget

        keys << k
        total -= weight.call(k, n)
      end
      keys
    end

    # The stand-in for a heavy library model, filling its bounding box
    # (+bmin+, +bmax+ in mm, definition coordinates): an ellipsoid for
    # vegetation (+round+), a box for the rest. Closed face loops.
    def proxy_faces(bmin, bmax, round)
      unless round
        ring = ccw([[bmin[0], bmin[1]], [bmax[0], bmin[1]], [bmax[0], bmax[1]], [bmin[0], bmax[1]]])
        return prism_faces(ring, bmin[2], bmax[2])
      end

      w = bmax[0] - bmin[0]
      d = bmax[1] - bmin[1]
      h = bmax[2] - bmin[2]
      cx = (bmin[0] + bmax[0]) / 2.0
      cy = (bmin[1] + bmax[1]) / 2.0
      sphere_faces([0.0, 0.0], 0.5, 0.0, 8, 12).map do |f|
        f.map { |p| [cx + (p[0] * w), cy + (p[1] * d), bmin[2] + (p[2] * h)] }
      end
    end

    # Where a site object stands: on top of the paving or deck it falls on,
    # else on the ground.
    def site_base(at, polygons, ground, top = -PAVING_TOP_MM)
      on = polygons.any? { |poly| point_in_polygon?([at[0].to_f, at[1].to_f], poly.map { |p| [p[0].to_f, p[1].to_f] }) }
      on ? top : ground
    end

    # A fence along a polyline: one closed board per segment, +height+ tall,
    # centred on the line.
    def fence_parts(points, closed, ground, height, thickness = FENCE_THICKNESS_MM)
      pts = points.map { |p| [p[0].to_f, p[1].to_f] }
      raise InvalidParams, 'a fence needs at least 2 points' if pts.size < 2

      segs = pts.each_cons(2).to_a
      segs << [pts[-1], pts[0]] if closed && pts.size > 2
      segs.each_with_index.filter_map do |(a, b), i|
        next if dist(a, b) < 1.0

        n = left_normal(unit(sub(b, a)))
        h = thickness / 2.0
        ring = [add(a, scale(n, -h)), add(b, scale(n, -h)), add(b, scale(n, h)), add(a, scale(n, h))]
        { name: "cerco_#{i + 1}", faces: prism_faces(ccw(ring), ground, ground + height) }
      end
    end

    # A pool sunk into the ground: {coping:, walls:, floor:, water:, outer:}.
    # +outer+ is the ring the terrain must leave open. Every part is closed.
    def pool_parts(points, ground, depth, coping: POOL_COPING_MM)
      inner = ccw(points)
      raise InvalidParams, "pool depth must be greater than 0, got #{fmt_mm(depth)}" unless depth.positive?

      bottom = ground - depth
      coping_out = offset_polygon(inner, [coping, POOL_WALL_MM].max)
      wall_out = offset_polygon(inner, POOL_WALL_MM)
      water = ground - POOL_WATER_DROP_MM
      {
        coping: slab_faces(coping_out, [inner], ground, ground + 30.0)[:faces],
        walls: slab_faces(wall_out, [inner], bottom, ground)[:faces],
        floor: prism_faces(wall_out, bottom - POOL_FLOOR_MM, bottom),
        water: prism_faces(inner, bottom, water),
        outer: coping_out
      }
    end

    # Sectional garage door: frame on three sides and four stacked panels on
    # the outer face, no swing. Same frame as door_parts.
    def garage_door_parts(width, height, thickness, swing, config = CONFIG)
      face = config[:door_frame_face_mm]
      depth = [config[:door_frame_depth_mm], thickness].min
      side = swing == 'in' ? 1.0 : -1.0
      outer_y = side * thickness / 2.0
      frame_y = [outer_y, outer_y - (side * depth)].minmax
      leaf_y = [outer_y - (side * 10.0), outer_y - (side * 50.0)].minmax
      parts = [
        { name: 'marco_izq', material: :metal, min: [0.0, frame_y[0], 0.0], max: [face, frame_y[1], height] },
        { name: 'marco_der', material: :metal, min: [width - face, frame_y[0], 0.0], max: [width, frame_y[1], height] },
        { name: 'marco_sup', material: :metal, min: [face, frame_y[0], height - face], max: [width - face, frame_y[1], height] }
      ]
      gap = 10.0
      clear = height - face - config[:door_undercut_mm]
      panel = (clear - (3 * gap)) / 4.0
      4.times do |k|
        z0 = config[:door_undercut_mm] + (k * (panel + gap))
        parts << { name: "panel_#{k + 1}", material: :metal, min: [face, leaf_y[0], z0], max: [width - face, leaf_y[1], z0 + panel] }
      end
      parts
    end

    FIT_MODES = %w[footprint real native].freeze

    # How a library component fits a block (all mm, component-local box
    # [bmin, bmax]). A model is taken to face -y, the SketchUp Front view;
    # +rot+ (degrees CCW) turns one that does not until it does.
    #   footprint: furniture scaled uniformly to fit inside the block's w x d
    #   real:      furniture at its own size times +scale+
    # both turned to face the room (+y of the block), back on the block's back
    # edge and centred across it, the block origin being its back-left corner;
    #   native:    a site object at its own size times +scale+, centred on the
    #              block origin, long side along x like the SITIO_ blocks.
    # Returns {scale:, rotation_deg:, translate: [x, y, z]} to apply as
    # T(block) * R(block rotation) * T(translate) * R(rotation_deg) * S(scale) * T(-centre_of_box_bottom).
    def fit_component(bmin, bmax, fit, w: nil, d: nil, rot: 0.0, scale: 1.0)
      size = [0, 1, 2].map { |k| bmax[k] - bmin[k] }
      raise InvalidParams, 'library component is empty' if size[0] <= 0 || size[1] <= 0
      raise InvalidParams, "library fit must be one of #{FIT_MODES.join(', ')}, got #{fit.inspect}" unless FIT_MODES.include?(fit)

      if fit == 'native'
        turn = size[1] > size[0] * 1.2 ? 90.0 : 0.0
        return { scale: scale.to_f, rotation_deg: (turn + rot.to_f) % 360, translate: [0.0, 0.0, 0.0] }
      end

      total = (180.0 + rot.to_f) % 360
      sx, sy = (total % 180).zero? ? [size[0], size[1]] : [size[1], size[0]]
      s = fit == 'footprint' ? [w / sx, d / sy].min : scale.to_f
      { scale: s, rotation_deg: total, translate: [w / 2.0, sy * s / 2.0, 0.0] }
    end

    def fmt_mm(v) = format('%g mm', v.round(1))
  end
end
