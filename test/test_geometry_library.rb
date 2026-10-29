# frozen_string_literal: true

require_relative 'test_helper'

# The library map entries without SketchUp: source and cache paths, staleness,
# the front turn and the normalisation an imported model gets before caching.
class TestGeometryLibrary < Minitest::Test
  G = Plomada::Geometry
  DIR = File.expand_path('C:/mcp/biblioteca')

  def test_a_skp_entry_is_its_own_path
    paths = G.library_paths(DIR, { 'skp' => 'sketchup/bano/34_toilet.skp' })
    assert_equal File.expand_path('sketchup/bano/34_toilet.skp', DIR), paths[:skp]
    assert_nil paths[:src]
  end

  def test_a_src_entry_caches_next_to_the_source_unless_skp_names_the_cache
    paths = G.library_paths(DIR, { 'src' => 'polyhaven/sofa_03/sofa_03_1k.glb' })
    assert_equal File.expand_path('polyhaven/sofa_03/sofa_03_1k.glb', DIR), paths[:src]
    assert_equal File.expand_path('polyhaven/sofa_03/sofa_03_1k.skp', DIR), paths[:skp]
    named = G.library_paths(DIR, { 'src' => 'a/b.v1.dae', 'skp' => 'cache/b.skp' })
    assert_equal File.expand_path('cache/b.skp', DIR), named[:skp]
    assert_equal File.expand_path('a/b.v1.skp', DIR), G.library_paths(DIR, { 'src' => 'a/b.v1.dae' })[:skp]
  end

  def test_an_entry_without_a_model_or_with_a_non_skp_cache_has_no_paths
    assert_nil G.library_paths(DIR, { 'skp' => nil, 'fit' => 'massing' })
    assert_nil G.library_paths(DIR, { 'skp' => '  ' })
    assert_nil G.library_paths(DIR, 'double_bed')
    assert_nil G.library_paths(DIR, { 'src' => 'a.glb', 'skp' => 'a.dae' })
  end

  def test_source_formats_are_the_ones_sketchup_imports
    %w[a.glb a.GLTF b/c.dae f.stl].each { |f| assert G.library_source_ext?(f), f }
    %w[d.obj a.fbx a.blend a.usd a.skp a].each { |f| refute G.library_source_ext?(f), f }
  end

  def test_a_cache_is_stale_when_missing_or_older_than_its_source
    t = Time.at(1_000_000)
    assert G.library_stale?(nil, t)
    assert G.library_stale?(t - 1, t)
    refute G.library_stale?(t, t)
    refute G.library_stale?(t + 5, t)
    refute G.library_stale?(t, nil)
  end

  def test_the_front_turn_brings_each_front_to_minus_y
    { nil => 0.0, '-y' => 0.0, '+X' => 270.0, '+y' => 180.0, '-x' => 90.0 }.each do |front, turn|
      deg = G.library_front_turn(front)
      assert_equal turn, deg, front.inspect
      dir = { nil => [0, -1], '-y' => [0, -1], '+X' => [1, 0], '+y' => [0, 1], '-x' => [-1, 0] }[front]
      a = deg * Math::PI / 180
      assert_in_delta 0.0, (dir[0] * Math.cos(a)) - (dir[1] * Math.sin(a)), 1e-9
      assert_in_delta(-1.0, (dir[0] * Math.sin(a)) + (dir[1] * Math.cos(a)), 1e-9)
    end
    err = assert_raises(Plomada::InvalidParams) { G.library_front_turn('north') }
    assert_match(/front must be one of -y, \+x, \+y, -x/, err.message)
  end

  # Applies the returned T * R * S to a point, as LibraryPrep does in SketchUp.
  def apply(norm, p)
    s = norm[:scale]
    a = norm[:turn_deg] * Math::PI / 180
    x = p[0] * s
    y = p[1] * s
    [(x * Math.cos(a)) - (y * Math.sin(a)) + norm[:translate][0],
     (x * Math.sin(a)) + (y * Math.cos(a)) + norm[:translate][1], (p[2] * s) + norm[:translate][2]]
  end

  def test_normalising_puts_the_centre_of_the_bottom_on_the_origin
    bmin = [1000.0, 2000.0, 500.0]
    bmax = [3000.0, 2600.0, 1400.0]
    [[0.0, 1.0], [90.0, 1.0], [270.0, 10.0], [180.0, 0.5]].each do |turn, scale|
      norm = G.library_normalize(bmin, bmax, turn: turn, scale: scale)
      corners = [bmin[0], bmax[0]].product([bmin[1], bmax[1]], [bmin[2], bmax[2]]).map { |c| apply(norm, c) }
      lo = (0..2).map { |k| corners.map { |c| c[k] }.min }
      hi = (0..2).map { |k| corners.map { |c| c[k] }.max }
      assert_in_delta 0.0, (lo[0] + hi[0]) / 2, 1e-6, "turn #{turn}"
      assert_in_delta 0.0, (lo[1] + hi[1]) / 2, 1e-6, "turn #{turn}"
      assert_in_delta 0.0, lo[2], 1e-6, "turn #{turn}"
      assert_equal (0..2).map { |k| (hi[k] - lo[k]).round(6) }, norm[:size].map { |v| v.round(6) }, "turn #{turn}"
    end
  end

  def test_a_metre_model_scaled_to_millimetres_reports_its_size
    norm = G.library_normalize([-0.9, -0.4, 0.0], [0.9, 0.4, 0.8], scale: 1000)
    assert_equal [1800.0, 800.0, 800.0], norm[:size].map { |v| v.round(6) }
    assert_nil G.library_size_note(norm[:size])
  end

  def test_a_size_in_the_wrong_unit_is_noted
    assert_match(/1800000 mm across: probably imported in the wrong unit/, G.library_size_note([1_800_000.0, 1.0, 1.0]))
    assert_match(/2 mm across/, G.library_size_note([1.8, 0.8, 0.8]))
    assert_nil G.library_size_note([4600.0, 1800.0, 1450.0])
  end

  def test_an_empty_model_or_a_bad_scale_is_refused
    assert_raises(Plomada::InvalidParams) { G.library_normalize([0, 0, 0], [0, 0, 0]) }
    err = assert_raises(Plomada::InvalidParams) { G.library_normalize([0, 0, 0], [1, 1, 1], scale: 0) }
    assert_match(/src_scale must be greater than 0/, err.message)
  end

  def test_up_names_the_rotation_that_stands_a_lying_model_upright
    { '+y' => [0, 1, 0], '-y' => [0, -1, 0], '+x' => [1, 0, 0], '-x' => [-1, 0, 0], '-z' => [0, 0, -1], 'z' => [0, 0, 1],
      nil => [0, 0, 1] }.each do |up, v|
      axis, deg = G.library_up_rotation(up)
      a = deg * Math::PI / 180
      k = axis
      # Rodrigues: v cos + (k x v) sin + k (k.v)(1 - cos)
      cross = [(k[1] * v[2]) - (k[2] * v[1]), (k[2] * v[0]) - (k[0] * v[2]), (k[0] * v[1]) - (k[1] * v[0])]
      dot = (k[0] * v[0]) + (k[1] * v[1]) + (k[2] * v[2])
      r = (0..2).map { |i| (v[i] * Math.cos(a)) + (cross[i] * Math.sin(a)) + (k[i] * dot * (1 - Math.cos(a))) }
      assert_in_delta 1.0, r[2], 1e-9, up.inspect
    end
    err = assert_raises(Plomada::InvalidParams) { G.library_up_rotation('up') }
    assert_match(/up must be one of \+z, \+y, -y, \+x, -x, -z/, err.message)
  end

  def test_drop_matches_node_names_by_substring_ignoring_case
    assert G.library_drop?('Plane__0', ['plane'])
    assert G.library_drop?('Backdrop_Mesh', %w[ground backdrop])
    refute G.library_drop?('Cylinder_Material.001_0', ['plane'])
    refute G.library_drop?('', ['plane'])
    refute G.library_drop?('Plane', nil)
    refute G.library_drop?('Plane', [''])
  end
end
