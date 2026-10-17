# frozen_string_literal: true

require_relative 'test_helper'

# Railings (glass panels per facet, posts and bars, solid parapets) and
# planters (curb, soil, Poisson-disk plants).
class TestGeometryRailingsPlanters < Minitest::Test
  include GeometryAssertions
  G = Plomada::Geometry
  Q = Math.tan(Math::PI / 8.0)

  def railing(path, **extra) = Plomada::Plan.railing({ 'id' => 'B1', 'path' => path }.merge(extra.transform_keys(&:to_s)), 'railings[0]')
  def planter(outline, **extra) = Plomada::Plan.planter({ 'id' => 'J1', 'outline' => outline }.merge(extra.transform_keys(&:to_s)), 'planters[0]')

  def closed?(loops)
    pool = G::VertexPool.new(0.001)
    G.manifold_report(loops.map { |f| { outer: f.map { |p| pool.id(p) }, holes: [] } })[:manifold]
  end

  def test_glass_panels_on_a_90_degree_r_4000_balcony_edge_follow_every_facet
    rec = railing([[4000.0, 0.0, Q], [0.0, 4000.0]])
    slab = [G::Arc.polyline([[0.0, 0.0], [4000.0, 0.0, Q], [0.0, 4000.0]], closed: true)]
    res = G.railing_parts(rec, slab, 0.0)
    facets = G::Arc.facets(4000.0, Math::PI / 2)
    assert_equal 16, facets
    glass = res[:parts].select { |p| p[:material] == 'MAT_vidrio' }
    assert_equal facets, glass.size
    res[:line].each { |p| assert_in_delta 3950.0, G.norm(p), 0.5, 'inset 50 mm toward the slab' }
    glass.each { |p| assert closed?(p[:faces]), p[:name] }
    zs = glass.flat_map { |p| p[:faces].flatten(1).map { |q| q[2] } }
    assert_equal [0.0, 1100.0], zs.minmax
  end

  def test_a_straight_glass_run_splits_at_1500_and_insets_toward_the_slab
    above = [[[-100.0, 0.0], [5000.0, 0.0], [5000.0, 3000.0], [-100.0, 3000.0]]]
    res = G.railing_parts(railing([[0.0, 0.0], [4000.0, 0.0]]), above, 150.0)
    assert_equal 3, res[:parts].size
    assert_point [0.0, 50.0], res[:line][0]
    below = [[[-100.0, -3000.0], [5000.0, -3000.0], [5000.0, 0.0], [-100.0, 0.0]]]
    res = G.railing_parts(railing([[0.0, 0.0], [4000.0, 0.0]]), below, 150.0)
    assert_point [0.0, -50.0], res[:line][0]
    assert_equal 150.0, res[:parts].flat_map { |p| p[:faces].flatten(1) }.map { |q| q[2] }.min
  end

  def test_a_glass_railing_with_a_rail_puts_a_50_mm_rail_on_top
    res = G.railing_parts(railing([[0.0, 0.0], [3000.0, 0.0]], rail: true), [], 0.0)
    rail = res[:parts].find { |p| p[:name] == 'baranda_sup' }
    assert rail
    assert closed?(rail[:faces])
    assert_equal [1050.0, 1100.0], rail[:faces].flatten(1).map { |q| q[2] }.minmax
  end

  def test_bars_have_posts_every_1200_and_balusters_within_the_clear_gap
    res = G.railing_parts(railing([[0.0, 0.0], [6000.0, 0.0]], style: 'bars'), [], 0.0)
    posts = res[:parts].select { |p| p[:name].start_with?('poste_') }
    assert_equal 6, posts.size
    xs = res[:parts].reject { |p| p[:name] == 'pasamanos' }.map do |p|
      ring = p[:faces].first
      ring.sum { |q| q[0] } / ring.size
    end.sort
    radius = ->(x) { posts.any? { |p| (p[:faces].first.sum { |q| q[0] } / p[:faces].first.size - x).abs < 1e-6 } ? 20.0 : 8.0 }
    xs.each_cons(2) do |a, b|
      gap = b - a - radius.call(a) - radius.call(b)
      assert_operator gap, :<=, 110.0 + 1e-6
      assert_operator gap, :>, 0.0
    end
    rail = res[:parts].find { |p| p[:name] == 'pasamanos' }[:faces]
    assert closed?(rail)
    res[:parts].each { |p| assert_operator G.loops_volume(p[:faces]), :>, 0.0, "#{p[:name]} faces outward" }
  end

  def test_a_solid_parapet_is_a_closed_ring_on_a_closed_path
    rec = railing([[0.0, 0.0], [6000.0, 0.0], [6000.0, 4000.0, Q], [0.0, 4000.0]], closed: true, style: 'solid')
    ring = [G::Arc.polyline(rec['path'], closed: true)]
    res = G.railing_parts(rec, ring, 0.0)
    faces = res[:parts].first[:faces]
    assert closed?(faces)
  end

  def test_poisson_disk_keeps_the_spacing_and_is_deterministic
    poly = G::Arc.polyline([[0.0, 0.0], [6000.0, 0.0, Q], [9000.0, 3000.0], [0.0, 3000.0]], closed: true)
    a = G.poisson_disk(poly, 600.0, 42)
    b = G.poisson_disk(poly, 600.0, 42)
    assert_equal a, b
    refute_equal a, G.poisson_disk(poly, 600.0, 43)
    a.combination(2).each { |p, q| assert_operator G.dist(p, q), :>=, 600.0 - 1e-9 }
    a.each { |p| assert G.point_in_polygon?(p, poly) }
    # A maximal sampling covers the area: at least a third of the densest packing count.
    area = G.signed_area(poly)
    assert_operator a.size, :>, area / (Math::PI * 300.0 * 300.0) * 0.33
  end

  def test_a_planter_has_a_closed_curb_soil_and_plants_that_rebuild_identically
    rec = planter([[0.0, 0.0], [4000.0, 0.0, Q], [8000.0, 4000.0], [0.0, 4000.0]],
                  plants: { 'keys' => %w[hedge potted_plant], 'spacing' => 700 })
    lay = G.planter_layout(rec, 0.0)
    assert closed?(lay[:soil])
    pool = G::VertexPool.new(0.001)
    rep = G.manifold_report(lay[:curb].map { |f| { outer: f[:outer].map { |p| pool.id(p) }, holes: f[:holes].map { |h| h.map { |p| pool.id(p) } } } })
    assert rep[:manifold]
    refute_empty lay[:plants]
    lay[:plants].each do |pl|
      assert_equal 400.0, pl[:z]
      assert G.point_in_polygon?(pl[:at], lay[:inner])
      assert_includes %w[hedge potted_plant], pl[:key]
    end
    assert_equal lay[:plants], G.planter_layout(rec, 0.0)[:plants]
    other = Plomada::Plan.planter(rec.merge('id' => 'J2'), 'planters[1]')
    refute_equal lay[:plants], G.planter_layout(other, 0.0)[:plants], 'the id seeds the scatter'
  end

  def test_a_massing_plant_is_a_closed_sphere
    faces = G.sphere_faces([0.0, 0.0], 300.0, 0.0)
    assert closed?(faces)
    assert_operator G.loops_volume(faces), :>, 0.0
  end
end
