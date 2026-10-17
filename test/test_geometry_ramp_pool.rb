# frozen_string_literal: true

require_relative 'test_helper'

# Ramps (slope limit, ruled surface, sloped side walls), pools on any storey
# and round columns.
class TestGeometryRampPool < Minitest::Test
  include GeometryAssertions
  G = Plomada::Geometry
  Q = Math.tan(Math::PI / 8.0)

  def ramp(axis, z_end, **extra)
    Plomada::Plan.ramp({ 'id' => 'R1', 'axis' => axis, 'width' => 3000, 'z_end' => z_end }
                       .merge(extra.transform_keys(&:to_s)), 'ramps[0]')
  end

  def closed?(loops)
    pool = G::VertexPool.new(0.001)
    G.manifold_report(loops.map { |f| { outer: f.map { |p| pool.id(p) }, holes: [] } })[:manifold]
  end

  def holed_closed?(faces)
    pool = G::VertexPool.new(0.001)
    G.manifold_report(faces.map { |f| { outer: f[:outer].map { |p| pool.id(p) }, holes: f[:holes].map { |h| h.map { |p| pool.id(p) } } } })[:manifold]
  end

  def test_an_18_percent_ramp_is_accepted
    rec = ramp([[0.0, 0.0], [9000.0, 0.0]], -1620.0)
    lay = G.ramp_layout(rec)
    assert_in_delta 0.18, lay[:slope], 1e-12
    lay[:parts].each_value { |faces| assert closed?(faces) }
    top = lay[:parts]['rampa'].flatten(1).map { |p| p[2] }
    assert_equal [-1620.0 - 200.0, 0.0], top.minmax
  end

  def test_a_21_percent_ramp_is_refused_with_both_numbers
    err = assert_raises(Plomada::InvalidParams) { ramp([[0.0, 0.0], [9000.0, 0.0]], -1890.0) }
    assert_equal 'ramp R1 climbs 1890 mm over 9000 mm, a slope of 21 percent; the most is 20 percent', err.message
  end

  def test_a_curved_ramp_measures_its_slope_along_the_arc
    rec = ramp([[0.0, 0.0, Q], [6000.0, 6000.0]], 900.0)
    lay = G.ramp_layout(rec)
    length = G::Arc.length([0.0, 0.0], [6000.0, 6000.0], Q)
    assert_in_delta 900.0 / length, lay[:slope], 1e-9
    assert_in_delta 0.1, lay[:slope], 0.005, 'a 10 percent access ramp'
    lay[:parts].each_value { |faces| assert closed?(faces) }
    assert_operator G.signed_area(lay[:outline]), :>, 0.0
  end

  def test_a_raised_pool_on_an_upper_storey_stands_on_its_slab
    rec = Plomada::Plan.pool({ 'id' => 'P1', 'outline' => [[0, 0], [8000, 0], [8000, 3500], [0, 3500]] }, 'pools[0]')
    lay = G.pool_layout(rec, 0.0, true)
    assert lay[:raised]
    water = lay[:parts]['agua'][0]
    assert_equal [200.0, 1400.0], water.flatten(1).map { |p| p[2] }.minmax, 'water surface 1400 above the slab'
    assert holed_closed?(lay[:parts]['vaso'][0])
    assert holed_closed?(lay[:parts]['borde'][0])
    assert closed?(lay[:parts]['fondo'][0])
    walls = lay[:parts]['vaso'][0].flat_map { |f| f[:outer] }
    assert_equal 0.0, walls.map { |p| p[2] }.min, 'the basin stands on the slab'
  end

  def test_a_recessed_pool_on_the_ground_storey_is_sunk_below_its_base
    rec = Plomada::Plan.pool({ 'id' => 'P1', 'outline' => [[0, 0], [8000, 0], [8000, 3500], [0, 3500]] }, 'pools[0]')
    lay = G.pool_layout(rec, 0.0, false)
    refute lay[:raised]
    assert_operator lay[:parts]['agua'][0].flatten(1).map { |p| p[2] }.max, :<, 0.0
  end

  def test_round_columns_take_sides_from_the_chord_tolerance
    assert_equal 16, G.column_sides(50.0)
    assert_equal 48, G.column_sides(5000.0)
    sides = G.column_sides(600.0)
    assert_operator sides, :>=, 16
    rec = Plomada::Plan.column({ 'id' => 'C1', 'at' => [1000, 1000], 'diameter' => 1200 }, 'columns[0]')
    faces = G.column_faces(rec, 0.0, 4000.0)
    assert closed?(faces)
    assert_equal sides + 2, faces.size
    square = Plomada::Plan.column({ 'id' => 'C2', 'outline' => [[0, 0], [300, 0], [300, 600], [0, 600]] }, 'columns[1]')
    assert_equal 6, G.column_faces(square, 0.0, 3000.0).size
  end
end
