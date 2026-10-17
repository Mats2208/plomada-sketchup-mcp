# frozen_string_literal: true

require_relative 'test_helper'

# Curtain walls (mullions, transoms, panes, the void they cut in a wall) and
# arched openings on straight walls.
class TestGeometryCurtainArch < Minitest::Test
  include GeometryAssertions
  G = Plomada::Geometry
  Q = Math.tan(Math::PI / 8.0)

  def plan(raw) = Plomada::Plan.normalize(raw)

  def assert_closed(layout, msg = nil)
    rep = G.manifold_report(layout[:faces].map { |f| { outer: f[:outer_ids], holes: f[:hole_ids] } })
    assert rep[:manifold], "#{msg} not manifold: #{rep.inspect}"
    assert_empty G.internal_faces(layout[:faces], layout[:solids], 1.0), "#{msg} has internal faces"
  end

  def closed_part?(loops)
    pool = G::VertexPool.new(0.001)
    G.manifold_report(loops.map { |f| { outer: f.map { |p| pool.id(p) }, holes: [] } })[:manifold] && G.loops_volume(loops).positive?
  end

  def wall = { 'id' => 'A', 'axis' => [[0, 0], [10_000, 0]], 'thickness' => 250 }

  def test_a_curtain_wall_divides_each_segment_equally_at_most_1200_apart
    rec = Plomada::Plan.curtain_wall({ 'id' => 'M1', 'path' => [[0, 0], [6000, 0]], 'transoms' => [1100] }, 'curtain_walls[0]')
    res = G.curtain_wall_parts(rec, 2800.0)
    assert_equal 6, res[:mullions]
    assert_equal 10, res[:panes], 'five spans, split by one transom'
    res[:parts].each { |p| assert closed_part?(p[:faces]), p[:name] }
    curved = Plomada::Plan.curtain_wall({ 'id' => 'M2', 'path' => [[4000, 0, Q], [0, 4000]] }, 'curtain_walls[1]')
    pts = G.mullion_points(curved, 1200.0)
    assert_equal 7, pts.size, '6283 mm of arc in six equal spans'
    pts.each { |p| assert_in_delta 4000.0, G.norm(p), 1e-6 }
  end

  def test_a_curtain_wall_inside_a_wall_replaces_its_cells
    pl = plan('walls' => [wall], 'curtain_walls' => [{ 'id' => 'M1', 'path' => [[2000, 0], [8000, 0]] }])
    voids = G.curtain_voids(pl['walls'], pl['curtain_walls'], 2800.0)
    assert_equal 1, voids.size
    assert_equal ['A', 2000.0, 6000.0, 0.0, 2800.0], voids[0].values_at('wall', 'offset', 'width', 'sill', 'height')
    lay = G.solve_walls(pl['walls'], G.wall_openings(pl, 2800.0), storey_height: 2800.0)
    assert_closed(lay, 'curtain void')
    assert_empty lay[:openings], 'a curtain wall places no opening component'
    xs = lay[:faces].select { |f| f[:part] == 'left' }.flat_map { |f| f[:outer] }.map { |p| p[0] }
    assert(xs.none? { |x| x > 2000.0 + 1e-6 && x < 8000.0 - 1e-6 }, 'no wall face inside the curtain wall')
  end

  def test_a_curved_curtain_wall_on_a_curved_wall_cuts_it_on_the_same_circle
    host = { 'id' => 'H', 'axis' => [[4000, 0, Q], [0, 4000]], 'thickness' => 200 }
    pl = plan('walls' => [host], 'curtain_walls' => [{ 'id' => 'M1', 'path' => [[4000, 0, Q], [0, 4000]] }])
    voids = G.curtain_voids(pl['walls'], pl['curtain_walls'], 2800.0)
    assert_equal 'H', voids[0]['wall']
    assert_in_delta 2000.0 * Math::PI, voids[0]['width'], 1e-6
    lay = G.solve_walls(pl['walls'], G.wall_openings(pl, 2800.0), storey_height: 2800.0)
    assert_closed(lay, 'curved curtain')
  end

  def test_a_curtain_wall_free_of_walls_cuts_nothing
    pl = plan('walls' => [wall], 'curtain_walls' => [{ 'id' => 'M1', 'path' => [[0, 3000], [6000, 3000]] }])
    assert_empty G.curtain_voids(pl['walls'], pl['curtain_walls'], 2800.0)
  end

  def test_an_arched_window_on_a_straight_wall_is_watertight
    pl = plan('walls' => [wall], 'openings' => [{ 'id' => 'W1', 'wall' => 'A', 'opening_kind' => 'window', 'offset' => 2000,
                                                  'width' => 1900, 'sill' => 500, 'height' => 1900, 'head' => 'arch' }])
    assert_equal 950.0, pl['openings'][0]['arch_rise'], 'a semicircle by default'
    lay = G.solve_walls(pl['walls'], pl['openings'], storey_height: 2800.0)
    assert_closed(lay, 'arched window')
    soffits = lay[:faces].select { |f| f[:part] == 'soffit' }
    assert_equal G::Arc.facets(950.0, Math::PI), soffits.size
    soffits.flat_map { |f| f[:outer] }.each do |p|
      assert_in_delta 950.0, Math.hypot(p[0] - 2950.0, p[2] - 1450.0), 1e-6, 'soffit vertices on the arch circle'
    end
    assert_equal 1, lay[:openings].size
  end

  def test_a_segmental_arch_and_an_arched_door
    pl = plan('walls' => [wall], 'openings' => [
                { 'id' => 'W1', 'wall' => 'A', 'opening_kind' => 'window', 'offset' => 1000, 'width' => 2400, 'sill' => 300,
                  'height' => 2100, 'head' => 'arch', 'arch_rise' => 500 },
                { 'id' => 'D1', 'wall' => 'A', 'opening_kind' => 'door', 'offset' => 6000, 'width' => 1000, 'height' => 2400,
                  'head' => 'arch', 'arch_rise' => 500 }
              ])
    lay = G.solve_walls(pl['walls'], pl['openings'], storey_height: 2800.0)
    assert_closed(lay, 'segmental arch and arched door')
  end

  def test_an_arch_crown_at_the_wall_top_is_refused
    pl = plan('walls' => [wall], 'openings' => [{ 'id' => 'W1', 'wall' => 'A', 'opening_kind' => 'window', 'offset' => 2000,
                                                  'width' => 1900, 'sill' => 900, 'height' => 1900, 'head' => 'arch' }])
    err = assert_raises(Plomada::InvalidParams) { G.solve_walls(pl['walls'], pl['openings'], storey_height: 2800.0) }
    assert_match(/arch crown at 2800 reaches the top of wall A/, err.message)
  end

  def test_an_arch_rise_above_half_the_width_is_refused
    err = assert_raises(Plomada::InvalidParams) do
      plan('walls' => [wall], 'openings' => [{ 'id' => 'W1', 'wall' => 'A', 'opening_kind' => 'window', 'offset' => 0,
                                               'width' => 1000, 'head' => 'arch', 'arch_rise' => 600 }])
    end
    assert_equal 'openings[0].arch_rise must be at most half the width (500), got 600', err.message
  end

  def test_arched_components_are_closed_solids
    [G.arch_window_parts(1900.0, 1900.0, 950.0), G.arch_window_parts(2400.0, 2100.0, 500.0),
     G.arch_door_parts(1000.0, 2400.0, 500.0, 250.0, 'in', 'left')].each do |parts|
      parts.each do |pt|
        loops = pt[:faces] || G.box_faces(pt[:min], pt[:max])
        assert closed_part?(loops), pt[:name]
      end
    end
  end
end
