# frozen_string_literal: true

require_relative 'test_helper'

# Site parts (paving, fences, pools), the garage door and library fitting.
class TestGeometrySite < Minitest::Test
  G = Plomada::Geometry

  def manifold?(faces)
    pool = G::VertexPool.new(0.001)
    loops = faces.map do |f|
      f.is_a?(Hash) ? { outer: f[:outer].map { |p| pool.id(p) }, holes: f[:holes].map { |h| h.map { |p| pool.id(p) } } } : { outer: f.map { |p| pool.id(p) }, holes: [] }
    end
    G.manifold_report(loops)[:manifold]
  end

  def zs(faces) = faces.flat_map { |f| f.is_a?(Hash) ? f[:outer] : f }.map { |p| p[2] }

  def test_paving_is_a_closed_slab_from_the_ground_to_just_under_the_floor
    faces = G.paving_faces([[0, 0], [6000, 0], [6000, 3000], [0, 3000]], -150.0)
    assert manifold?(faces)
    assert_equal [-150.0, -20.0], zs(faces).minmax
  end

  def test_clockwise_paving_is_accepted
    assert manifold?(G.paving_faces([[0, 0], [0, 3000], [6000, 3000], [6000, 0]], -150.0))
  end

  def test_fence_has_one_closed_board_per_segment
    parts = G.fence_parts([[0, 0], [10_000, 0], [10_000, 8000]], false, -150.0, 1800.0)
    assert_equal %w[cerco_1 cerco_2], parts.map { |p| p[:name] }
    parts.each { |p| assert manifold?(p[:faces]), p[:name] }
    assert_in_delta 1650.0, zs(parts[0][:faces]).max, 1e-9
    closed = G.fence_parts([[0, 0], [1000, 0], [1000, 1000], [0, 1000]], true, 0.0, 1200.0)
    assert_equal 4, closed.size
  end

  def test_pool_parts_are_closed_and_leave_the_coping_ring_open_in_the_terrain
    pool = G.pool_parts([[0, 0], [8000, 0], [8000, 4000], [0, 4000]], -150.0, 1500.0)
    %i[coping walls floor water].each { |k| assert manifold?(pool[k]), k }
    xs = pool[:outer].map { |p| p[0] }
    assert_in_delta(-300.0, xs.min, 1e-6)
    assert_in_delta 8300.0, xs.max, 1e-6
    assert_in_delta(-400.0, zs(pool[:water]).max, 1e-9, 'water 250 mm below the ground')
    assert_in_delta(-1650.0, zs(pool[:water]).min, 1e-9)
  end

  def test_garage_door_is_a_frame_and_four_panels
    parts = G.garage_door_parts(2700.0, 2200.0, 200.0, 'out')
    assert_equal %w[marco_izq marco_der marco_sup panel_1 panel_2 panel_3 panel_4], parts.map { |p| p[:name] }
    top = parts.select { |p| p[:name].start_with?('panel') }.map { |p| p[:max][2] }.max
    assert_operator top, :<=, 2200.0 - Plomada::CONFIG[:door_frame_face_mm] + 1e-9
  end

  def test_footprint_fit_faces_the_room_and_scales_inside
    # A bed modelled facing -y, 1600 wide and 2000 long, for a 1600 x 2000 block.
    fit = G.fit_component([0, 0, 0], [1600, 2000, 900], 'footprint', w: 1600.0, d: 2000.0)
    assert_in_delta 180.0, fit[:rotation_deg], 1e-9
    assert_in_delta 1.0, fit[:scale], 1e-9
    assert_equal [800.0, 1000.0, 0.0], fit[:translate]
    # A model drawn in the wrong units (ten times too big) is brought down to the block.
    big = G.fit_component([0, 0, 0], [16_000, 20_000, 9000], 'footprint', w: 1600.0, d: 2000.0)
    assert_in_delta 0.1, big[:scale], 1e-9
  end

  def test_real_fit_keeps_the_size_and_puts_the_back_on_the_wall
    # An 800 x 700 fridge in a 600 x 650 block: real size, back on y 0, centred across.
    fit = G.fit_component([0, 0, 0], [800, 700, 1700], 'real', w: 600.0, d: 650.0)
    assert_in_delta 1.0, fit[:scale], 1e-9
    assert_equal [300.0, 350.0, 0.0], fit[:translate]
  end

  def test_rot_turns_a_model_that_faces_sideways
    # A toilet modelled 760 deep along x: rot 90 brings its front to -y, then it faces the room.
    fit = G.fit_component([0, 0, 0], [760, 520, 790], 'real', w: 380.0, d: 700.0, rot: 90)
    assert_in_delta 270.0, fit[:rotation_deg], 1e-9
    assert_in_delta 380.0, fit[:translate][1], 1e-9
    sized = G.fit_component([0, 0, 0], [1000, 500, 400], 'real', w: 1000.0, d: 500.0, scale: 1.5)
    assert_in_delta 1.5, sized[:scale], 1e-9
  end

  def test_an_unknown_fit_is_refused_by_name
    err = assert_raises(Plomada::InvalidParams) { G.fit_component([0, 0, 0], [1, 1, 1], 'stretch') }
    assert_match(/footprint, real, native/, err.message)
  end

  def test_native_fit_keeps_the_size
    fit = G.fit_component([0, 0, 0], [4600, 1800, 1450], 'native', rot: 90)
    assert_equal 1.0, fit[:scale]
    assert_in_delta 90.0, fit[:rotation_deg], 1e-9
  end

  def test_native_fit_turns_a_model_that_is_long_along_y
    fit = G.fit_component([0, 0, 0], [1800, 4600, 1450], 'native')
    assert_in_delta 90.0, fit[:rotation_deg], 1e-9
  end

  def test_an_object_on_paving_stands_on_its_top
    drive = [[0, 0], [3000, 0], [3000, 6000], [0, 6000]]
    assert_equal(-20.0, G.site_base([1500, 3000], [drive], -150.0))
    assert_equal(-150.0, G.site_base([5000, 3000], [drive], -150.0))
  end

  def test_the_heaviest_site_models_go_to_massing_first_until_the_budget_holds
    counts = { 'hedge' => 32, 'tree' => 104, 'car_sedan' => 44, 'pine' => 50 }
    faces = { 'hedge' => 537_600, 'tree' => 66_586, 'car_sedan' => 111_922, 'pine' => 10_200 }
    # 17.2 M + 6.9 M + 4.9 M + 0.5 M = 29.5 M; dropping the hedges leaves 12.3 M, then the ficus 5.4 M
    assert_equal %w[hedge tree], G.site_proxy_keys(counts, faces, 8_000_000)
    assert_equal [], G.site_proxy_keys(counts, faces, 30_000_000)
    assert_equal %w[hedge tree car_sedan pine], G.site_proxy_keys(counts, faces, 0)
  end

  def test_a_proxy_fills_the_models_box_and_is_closed
    box = G.proxy_faces([-1000, -500, 0], [1000, 500, 1500], false)
    blob = G.proxy_faces([-2000, -2000, 0], [2000, 2000, 6000], true)
    [box, blob].each { |faces| assert manifold?(faces) }
    pts = blob.flatten(1)
    assert_in_delta(-2000.0, pts.map { |p| p[0] }.min, 1.0)
    assert_in_delta(6000.0, pts.map { |p| p[2] }.max, 1.0)
    assert_in_delta(0.0, pts.map { |p| p[2] }.min, 1.0)
    assert_equal [0.0, 1500.0], zs(box).minmax
  end
end
