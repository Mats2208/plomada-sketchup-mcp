# frozen_string_literal: true

require_relative 'test_helper'

# Geometry::Arc: the DXF bulge convention, arc length, offsets and the
# tessellation every curved element shares.
class TestGeometryArc < Minitest::Test
  include GeometryAssertions

  Arc = Plomada::Geometry::Arc

  def test_center_and_radius_of_a_semicircle
    c, r = Arc.center_radius([0.0, 0.0], [2000.0, 0.0], 1.0)
    assert_point [1000.0, 0.0], c
    assert_in_delta 1000.0, r, 1e-9
    assert_in_delta Math::PI, Arc.sweep(1.0), 1e-12
  end

  def test_center_and_radius_of_a_quarter_circle
    # tan(90 / 4 degrees) = 0.414214: a quarter circle of radius 2000 from (2000, 0) to (0, 2000), CCW.
    b = Math.tan(Math::PI / 8.0)
    assert_in_delta 0.414214, b, 1e-6
    c, r = Arc.center_radius([2000.0, 0.0], [0.0, 2000.0], b)
    assert_point [0.0, 0.0], c, 1e-9
    assert_in_delta 2000.0, r, 1e-9
    assert_in_delta 1000.0 * Math::PI, Arc.length([2000.0, 0.0], [0.0, 2000.0], b), 1e-6
  end

  def test_center_and_radius_of_a_shallow_arc
    # bulge 0.1: theta = 4 atan(0.1) = 22.84 degrees; r = c (1 + b^2) / (4 b).
    c, r = Arc.center_radius([0.0, 0.0], [1000.0, 0.0], 0.1)
    assert_in_delta 1000.0 * 1.01 / 0.4, r, 1e-9
    assert_in_delta 500.0, c[0], 1e-9
    assert_operator c[1], :>, 0.0, 'a counter-clockwise arc has its center on the left of the chord'
    assert_in_delta r, Plomada::Geometry.dist(c, [0.0, 0.0]), 1e-9
    _, r2 = Arc.center_radius([0.0, 0.0], [1000.0, 0.0], -0.1)
    assert_in_delta r, r2, 1e-9
    c2, = Arc.center_radius([0.0, 0.0], [1000.0, 0.0], -0.1)
    assert_operator c2[1], :<, 0.0, 'a clockwise arc has its center on the right'
  end

  def test_point_at_the_ends_and_the_middle
    b = Math.tan(Math::PI / 8.0)
    p0 = [2000.0, 0.0]
    p1 = [0.0, 2000.0]
    len = Arc.length(p0, p1, b)
    assert_point p0, Arc.point_at(p0, p1, b, 0.0), 1e-9
    assert_point p1, Arc.point_at(p0, p1, b, len), 1e-9
    h = 2000.0 / Math.sqrt(2.0)
    assert_point [h, h], Arc.point_at(p0, p1, b, len / 2.0), 1e-9
    assert_point [0.0, 1.0], Arc.tangent_at(p0, p1, b, 0.0), 1e-12
    assert_point [-1.0, 0.0], Arc.tangent_at(p0, p1, b, len), 1e-12
    assert_point [1.0, 0.0], Arc.tangent_at(p1, p0, -b, 0.0), 1e-12
  end

  def test_point_at_on_a_straight_segment
    assert_point [300.0, 400.0], Arc.point_at([0.0, 0.0], [600.0, 800.0], 0.0, 500.0)
  end

  def test_offset_radius_moves_toward_the_center_on_the_left_of_a_ccw_arc
    b = Math.tan(Math::PI / 8.0)
    q0, q1, qb = Arc.offset([2000.0, 0.0], [0.0, 2000.0], b, 100.0)
    assert_point [1900.0, 0.0], q0, 1e-9
    assert_point [0.0, 1900.0], q1, 1e-9
    assert_equal b, qb
    _, r = Arc.center_radius(q0, q1, qb)
    assert_in_delta 1900.0, r, 1e-9
    # Clockwise: the left side is away from the center.
    q0, q1, qb = Arc.offset([0.0, 2000.0], [2000.0, 0.0], -b, 100.0)
    _, r = Arc.center_radius(q0, q1, qb)
    assert_in_delta 2100.0, r, 1e-9
  end

  def test_offset_refuses_a_radius_under_50_mm
    b = Math.tan(Math::PI / 8.0)
    err = assert_raises(Plomada::InvalidParams) { Arc.offset([200.0, 0.0], [0.0, 200.0], b, 170.0) }
    assert_match(/offset of 170 mm from an arc of radius 200 mm leaves radius 30 mm, under the 50 mm minimum/, err.message)
  end

  def test_tessellation_sagitta_never_exceeds_the_tolerance_over_200_random_arcs
    rng = Random.new(1337)
    worst = 0.0
    200.times do
      p0 = [rng.rand(-20_000.0..20_000.0), rng.rand(-20_000.0..20_000.0)]
      ang = rng.rand(0.0..(2 * Math::PI))
      chord = rng.rand(200.0..20_000.0)
      p1 = [p0[0] + (chord * Math.cos(ang)), p0[1] + (chord * Math.sin(ang))]
      b = rng.rand(0.02..1.0) * (rng.rand < 0.5 ? -1.0 : 1.0)
      pts = Arc.tessellate(p0, p1, b)
      c, r = Arc.center_radius(p0, p1, b)
      assert_equal p0, pts.first
      assert_equal p1, pts.last
      assert_operator pts.size - 1, :>=, Plomada::CONFIG[:arc_min_facets]
      pts.each { |q| assert_in_delta r, Plomada::Geometry.dist(q, c), 1e-6 }
      pts.each_cons(2) do |a, q|
        mid = Plomada::Geometry.scale(Plomada::Geometry.add(a, q), 0.5)
        worst = [worst, r - Plomada::Geometry.dist(mid, c)].max
      end
    end
    assert_operator worst, :<=, 5.0
    assert_operator worst, :>, 1.0, 'the tolerance is used, not wasted on needless facets'
  end

  def test_facet_count_is_clamped
    assert_equal 4, Arc.facets(100.0, 0.1)
    assert_equal 96, Arc.facets(1_000_000.0, Math::PI)
  end

  def test_a_bulge_above_a_semicircle_is_refused_by_segment
    err = assert_raises(Plomada::InvalidParams) { Arc.check_bulge!(Math.tan(200.0 * Math::PI / 720.0), 2, 'wall M1') }
    assert_equal 'segment 2 of wall M1 sweeps 200 degrees; split arcs above 180 degrees', err.message
    Arc.check_bulge!(1.0, 0, 'wall M1')
  end

  def test_polyline_tessellates_arcs_and_closes_rings
    ring = [[0.0, 0.0], [2000.0, 0.0, 1.0], [2000.0, 2000.0], [0.0, 2000.0]]
    pts = Arc.polyline(ring, closed: true)
    assert_equal [0.0, 0.0], pts.first
    refute_equal pts.first, pts.last
    assert(pts.any? { |p| p[0] > 2900.0 }, 'the semicircle bulges out to x 3000')
    open = Arc.polyline([[0.0, 0.0], [1000.0, 0.0, 0.5], [2000.0, 0.0]])
    assert_equal [2000.0, 0.0], open.last
    assert Arc.curved?(ring)
    refute Arc.curved?([[0.0, 0.0], [1.0, 0.0, 0.0]])
  end
end
