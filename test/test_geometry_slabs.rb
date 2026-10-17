# frozen_string_literal: true

require_relative 'test_helper'

# Slab records: unions per top elevation, holes and wells, edge bands that
# follow curves, and the plane booleans underneath.
class TestGeometrySlabs < Minitest::Test
  include GeometryAssertions
  G = Plomada::Geometry
  Q = Math.tan(Math::PI / 8.0)

  def slab(id, outline, **extra)
    raw = { 'id' => id, 'outline' => outline }.merge(extra.transform_keys(&:to_s))
    Plomada::Plan.slab(raw, "slabs[#{id}]")
  end

  def rect(x0, y0, x1, y1) = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

  def closed?(faces)
    pool = G::VertexPool.new(0.001)
    loops = faces.map { |f| { outer: f[:outer].map { |p| pool.id(p) }, holes: f[:holes].map { |h| h.map { |p| pool.id(p) } } } }
    G.manifold_report(loops)[:manifold]
  end

  def region_area(region) = G::Boolean.area(region)

  def test_two_overlapping_slabs_union_into_one_top_face
    plate = slab('L1', rect(0.0, 0.0, 6000.0, 4000.0))
    balcony = slab('B1', rect(5000.0, 1000.0, 8000.0, 3000.0))
    lay = G.slab_layout([plate, balcony])
    assert_equal 1, lay[:slabs].size
    s = lay[:slabs].first
    assert_equal %w[L1 B1], s[:ids]
    assert_equal 1, s[:region].size
    assert_empty s[:region].first[:holes]
    assert_in_delta 28_000_000.0, region_area(s[:region]), 1e-3
    tops = s[:faces].select { |f| f[:normal] == [0.0, 0.0, 1.0] }
    assert_equal 1, tops.size, 'one top face, no coplanar pair'
    assert closed?(s[:faces])
    assert_in_delta(-150.0, s[:faces].flat_map { |f| f[:outer] }.map { |p| p[2] }.min, 1e-9)
  end

  def test_slabs_that_only_touch_share_their_edge
    lay = G.slab_layout([slab('A', rect(0.0, 0.0, 3000.0, 3000.0)), slab('B', rect(3000.0, 1000.0, 5000.0, 2000.0))])
    region = lay[:slabs].first[:region]
    assert_equal 1, region.size
    assert_in_delta 11_000_000.0, region_area(region), 1e-3
    assert closed?(lay[:slabs].first[:faces])
  end

  def test_a_curved_balcony_is_tessellated_on_the_shared_kernel
    out = [[0.0, 0.0], [4000.0, 0.0, Q], [8000.0, 4000.0], [0.0, 4000.0]]
    lay = G.slab_layout([slab('L1', out)])
    ring = lay[:slabs].first[:region].first[:outer]
    expected = G::Arc.tessellate([4000.0, 0.0], [8000.0, 4000.0], Q)
    expected.each { |p| assert(ring.any? { |q| G.dist(p, q) < 1e-6 }, "arc point #{p.inspect} on the slab edge") }
  end

  def test_holes_and_wells_are_cut_and_a_hole_under_another_slab_is_filled
    plate = slab('L1', rect(0.0, 0.0, 10_000.0, 8000.0), holes: [rect(1000.0, 1000.0, 3000.0, 3000.0)])
    lay = G.slab_layout([plate], [rect(6000.0, 4000.0, 8000.0, 7000.0)])
    region = lay[:slabs].first[:region]
    assert_equal 2, region.first[:holes].size
    assert_in_delta 80_000_000.0 - 4_000_000.0 - 6_000_000.0, region_area(region), 1e-3
    assert closed?(lay[:slabs].first[:faces])
    patch = slab('P', rect(500.0, 500.0, 3500.0, 3500.0))
    filled = G.slab_layout([plate, patch])[:slabs].first[:region]
    assert_empty filled.first[:holes]
  end

  def test_different_top_offsets_build_separate_slabs
    lay = G.slab_layout([slab('L1', rect(0.0, 0.0, 6000.0, 4000.0)),
                         slab('T1', rect(6000.0, 0.0, 9000.0, 4000.0), top_offset: -150.0)])
    assert_equal [0.0, -150.0], lay[:slabs].map { |s| s[:top] }
  end

  def test_an_edge_band_follows_a_curved_outline
    out = [[0.0, 0.0], [4000.0, 0.0, Q], [8000.0, 4000.0], [0.0, 4000.0]]
    s = slab('B1', out, edge_band: { 'height' => 450.0, 'depth' => 150.0 })
    lay = G.slab_layout([s])
    band = lay[:bands].first
    assert_equal 'B1', band[:id]
    assert_in_delta(-150.0, band[:top], 1e-9)
    assert_in_delta(-600.0, band[:bottom], 1e-9)
    ring = band[:region].first[:outer]
    slab_ring = lay[:slabs].first[:region].first[:outer]
    ring.each { |p| assert(slab_ring.any? { |q| G.dist(p, q) < 1e-6 }, "band edge point #{p.inspect} on the slab edge") }
    inner = band[:region].first[:holes].first
    c = [4000.0, 4000.0] # center of the quarter circle
    arc_inner = inner.select { |p| p[0] > 4000.0 + 1.0 && p[1] < 4000.0 - 1.0 }
    refute_empty arc_inner
    arc_inner.each { |p| assert_operator((G.dist(p, c) - 3850.0).abs, :<, 5.0) }
    assert closed?(band[:faces])
  end

  def test_a_band_does_not_run_along_an_edge_shared_with_another_slab
    plate = slab('L1', rect(0.0, 0.0, 6000.0, 4000.0))
    balcony = slab('B1', rect(6000.0, 1000.0, 8000.0, 3000.0), edge_band: { 'height' => 300.0, 'depth' => 100.0 })
    band = G.slab_layout([plate, balcony])[:bands].first
    pts = band[:region].flat_map { |r| r[:outer] }
    assert(pts.none? { |p| p[0] < 6000.0 - 1e-6 }, 'no band under the floor plate')
    assert_in_delta (1900.0 + 1900.0 + 1800.0) * 100.0, region_area(band[:region]), 1.0
  end

  def test_the_boolean_keeps_two_disjoint_islands_apart
    region = G::Boolean.region([{ outer: rect(0.0, 0.0, 1000.0, 1000.0) }, { outer: rect(2000.0, 0.0, 3000.0, 1000.0) }])
    assert_equal 2, region.size
  end

  def test_the_boolean_splits_a_bow_tie_into_two_simple_rings
    region = G::Boolean.region([{ outer: rect(0.0, 0.0, 1000.0, 1000.0) }, { outer: rect(1000.0, 1000.0, 2000.0, 2000.0) }])
    assert_equal 2, region.size
    region.each { |r| assert_equal 4, r[:outer].size }
  end
end
