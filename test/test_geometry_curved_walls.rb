# frozen_string_literal: true

require_relative 'test_helper'

# Curved walls: arc segments in the wall solver, their joints with lines and
# other arcs, T junctions onto an arc, openings split into flat panels, and
# the refusals. Every layout must be a closed manifold with no internal face.
class TestGeometryCurvedWalls < Minitest::Test
  include GeometryAssertions
  G = Plomada::Geometry
  Q = Math.tan(Math::PI / 8.0) # bulge of a quarter circle

  def plan(walls, openings = [])
    Plomada::Plan.normalize({ 'walls' => walls, 'openings' => openings })
  end

  def wall(id, axis, thickness: 200.0, closed: false, justification: 'center')
    { 'id' => id, 'axis' => axis, 'thickness' => thickness, 'closed' => closed, 'justification' => justification }
  end

  def window(id, wall, offset, width, kind: 'window', **extra)
    { 'id' => id, 'wall' => wall, 'opening_kind' => kind, 'offset' => offset, 'width' => width }
      .merge(extra.transform_keys(&:to_s))
  end

  def solve(walls, openings = [])
    pl = plan(walls, openings)
    G.solve_walls(pl['walls'], pl['openings'], storey_height: 2800.0)
  end

  def assert_closed(layout, msg = nil)
    rep = G.manifold_report(layout[:faces].map { |f| { outer: f[:outer_ids], holes: f[:hole_ids] } })
    assert rep[:manifold], "#{msg} not manifold: #{rep.inspect}"
    assert_empty G.internal_faces(layout[:faces], layout[:solids], 1.0), "#{msg} has internal faces"
  end

  def radius(p, c = [0.0, 0.0]) = Math.hypot(p[0] - c[0], p[1] - c[1])

  def test_a_quarter_circle_wall_of_radius_2000_is_watertight
    lay = solve([wall('M1', [[2000.0, 0.0, Q], [0.0, 2000.0]])])
    assert_closed(lay, 'quarter circle')
    curved = lay[:faces].select { |f| f[:curved] }
    refute_empty curved
    # Every vertex of a curved face lies on the inner (1900) or outer (2100) circle.
    curved.flat_map { |f| f[:outer] }.each do |p|
      r = radius(p)
      assert((r - 1900.0).abs < 1e-6 || (r - 2100.0).abs < 1e-6, "vertex at radius #{r}")
    end
    lefts = curved.count { |f| f[:part] == 'left' }
    rights = curved.count { |f| f[:part] == 'right' }
    assert_equal lefts, rights
    assert_equal G::Arc.facets(2100.0, Math::PI / 2), lefts, 'one facet per station step'
    # The top is one face following the curve.
    tops = lay[:faces].select { |f| f[:part] == 'top' }
    assert_equal 1, tops.size
  end

  def test_the_facets_keep_the_chord_tolerance_on_the_outer_face
    lay = solve([wall('M1', [[2000.0, 0.0, Q], [0.0, 2000.0]])])
    outer = lay[:faces].select { |f| f[:curved] && f[:part] == 'right' }
    outer.each do |f|
      xs = f[:outer].map { |p| [p[0], p[1]] }.uniq
      a, b = xs
      mid = [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]
      assert_operator 2100.0 - radius(mid), :<=, 5.0 + 1e-9
    end
  end

  def test_a_1500_mm_window_on_radius_4000_splits_into_two_flat_panels
    lay = solve([wall('M1', [[4000.0, 0.0, Q], [0.0, 4000.0]])],
                [window('W1', 'M1', 1000.0, 1500.0, sill: 900.0, height: 1200.0)])
    assert_closed(lay, 'window on an arc')
    frames = lay[:openings]
    assert_equal 2, frames.size
    assert_equal [0, 1], frames.map { |f| f[:panel] }
    assert(frames.all? { |f| f[:id] == 'W1' && f[:panels] == 2 })
    chord = 2.0 * 4000.0 * Math.sin(750.0 / 4000.0 / 2.0)
    frames.each { |f| assert_in_delta chord, f[:width], 1e-6 }
    frames.each { |f| assert_in_delta 4000.0, radius(f[:origin]), 1e-6, 'panels start on the mid-thickness arc' }
    # The second panel starts where the first ends.
    first_end = G.add(frames[0][:origin][0, 2], G.scale(frames[0][:xaxis][0, 2], frames[0][:width]))
    assert_point first_end, frames[1][:origin][0, 2], 1e-6
  end

  def test_a_900_mm_door_on_an_arc_is_one_panel
    lay = solve([wall('M1', [[4000.0, 0.0, Q], [0.0, 4000.0]])],
                [window('D1', 'M1', 2000.0, 900.0, kind: 'door')])
    assert_closed(lay, 'door on an arc')
    assert_equal 1, lay[:openings].size
    assert_equal 'door', lay[:openings][0][:kind]
    assert_equal 0.0, lay[:openings][0][:sill]
  end

  def test_an_arc_to_line_l_joint_is_mitred_on_the_offset_curves
    walls = [wall('A', [[0.0, 0.0], [4000.0, 0.0]]), wall('B', [[4000.0, 0.0, 0.5], [4000.0, 4000.0]])]
    lay = solve(walls)
    assert_closed(lay, 'arc-line L')
    solver = G::WallSolver.new(plan(walls)['walls'], [], storey_height: 2800.0)
    solver.solve
    b = solver.segs.find { |s| s.id == 'B' }
    assert_equal :mitre, b.start_cut[:kind]
    a = solver.segs.find { |s| s.id == 'A' }
    # The mitre points lie on the line faces (y = +-100) and on B's face circles.
    [%i[l l], %i[r r]].each do |sa, _|
      pa = solver.to_world(a, [a.end_cut[sa], a.off(sa)])
      assert_in_delta a.off(sa), pa[1], 1e-6
    end
  end

  def test_a_tangent_line_to_arc_continues_without_a_mitre
    lay = solve([wall('A', [[0.0, 0.0], [4000.0, 0.0, Q], [6000.0, 2000.0], [6000.0, 6000.0]])])
    assert_closed(lay, 'tangent line-arc-line')
    ends = lay[:faces].select { |f| f[:part] == 'end' }
    assert_equal 2, ends.size, 'only the two free ends have end faces'
  end

  def test_an_s_joint_of_two_arcs_is_tangent
    lay = solve([wall('S', [[0.0, 0.0, Q], [4000.0, 0.0, -Q], [8000.0, 0.0]])])
    assert_closed(lay, 'S curve')
    assert_equal 2, lay[:faces].count { |f| f[:part] == 'end' }
  end

  def test_two_arcs_meeting_at_a_corner_are_mitred_circle_to_circle
    lay = solve([wall('C', [[0.0, 0.0, Q], [4000.0, 0.0, Q], [8000.0, 0.0]])])
    assert_closed(lay, 'arc-arc corner')
  end

  def test_a_straight_wall_tees_into_an_arc
    host = wall('H', [[4000.0, 0.0, Q], [0.0, 4000.0]])
    h = 4000.0 / Math.sqrt(2.0)
    stem = wall('T', [[1000.0, 1000.0], [h, h]], thickness: 120.0)
    walls = [host, stem]
    lay = solve(walls)
    assert_closed(lay, 'T onto an arc')
    solver = G::WallSolver.new(plan(walls)['walls'], [], storey_height: 2800.0)
    solver.solve
    t = solver.segs.find { |s| s.id == 'T' }
    assert_equal :tee, t.end_cut[:kind]
    # The stem is trimmed to the host's inner circle (radius 3900).
    %i[l r].each { |side| assert_in_delta 3900.0, radius(solver.to_world(t, [t.end_cut[side], t.off(side)])), 1e-6 }
    j = solver.segs.find { |s| s.id == 'H' }.junctions
    assert_equal 1, j.size
  end

  def test_a_round_tower_of_two_semicircles_has_a_circular_outline
    lay = solve([wall('R', [[0.0, -3000.0, 1.0], [0.0, 3000.0, 1.0]], closed: true)])
    assert_closed(lay, 'round tower')
    ext = lay[:exterior]
    assert_equal 'R', ext[:wall]
    ext[:points].each { |p| assert_in_delta 3100.0, radius(p), 1e-6 }
    area = G.signed_area(ext[:points])
    assert_in_delta Math::PI * 3100.0 * 3100.0, area, Math::PI * 3100.0 * 3100.0 * 0.003
  end

  def test_an_inner_radius_of_250_mm_is_refused
    err = assert_raises(Plomada::InvalidParams) { solve([wall('M1', [[350.0, 0.0, Q], [0.0, 350.0]])]) }
    assert_match(/wall M1 curves too tightly: its inner face has a radius of 250 mm .* the minimum is 300 mm/, err.message)
  end

  def test_a_200_degree_bulge_is_refused
    b = Math.tan(200.0 * Math::PI / 720.0)
    err = assert_raises(Plomada::InvalidParams) { plan([wall('M1', [[0.0, 0.0, b], [4000.0, 0.0]])]) }
    assert_equal 'segment 0 of wall M1 sweeps 200 degrees; split arcs above 180 degrees', err.message
  end

  def test_an_opening_across_the_vertex_between_a_line_and_an_arc_is_refused
    walls = [wall('A', [[0.0, 0.0], [4000.0, 0.0, Q], [6000.0, 2000.0]])]
    err = assert_raises(Plomada::InvalidParams) { solve(walls, [window('W1', 'A', 3500.0, 1000.0)]) }
    assert_match(/an opening must sit inside one straight run/, err.message)
  end

  def test_a_panel_that_bows_more_than_60_mm_is_refused_with_the_width_that_fits
    walls = [wall('M1', [[1000.0, 0.0, Q], [0.0, 1000.0]])]
    err = assert_raises(Plomada::InvalidParams) do
      solve(walls, [window('W1', 'M1', 100.0, 1200.0, panel_width: 1200.0)])
    end
    assert_match(/a 1200 mm panel bows 174\.7 mm off its chord at radius 1000 mm; the most is 60 mm, so set panel_width to 696 mm or less/,
                 err.message)
    lay = solve(walls, [window('W1', 'M1', 100.0, 1200.0, panel_width: 690.0)])
    assert_equal 2, lay[:openings].size
    assert_closed(lay, 'narrow panels')
  end

  def test_an_arched_head_on_an_arc_is_refused
    walls = [wall('M1', [[4000.0, 0.0, Q], [0.0, 4000.0]])]
    err = assert_raises(Plomada::InvalidParams) do
      solve(walls, [window('W1', 'M1', 1000.0, 1200.0, head: 'arch')])
    end
    assert_match(/arched head needs a straight wall/, err.message)
  end
end
