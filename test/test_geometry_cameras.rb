# frozen_string_literal: true

require_relative 'test_helper'

# Automatic scene cameras: one per room, four around the building.
class TestGeometryCameras < Minitest::Test
  G = Plomada::Geometry

  BOX = { 'id' => 'EXT', 'axis' => [[0, 0], [6000, 0], [6000, 4000], [0, 4000]], 'thickness' => 200.0,
          'justification' => 'center', 'closed' => true }.freeze

  def inside?(p, lo, hi) = p[0].between?(lo[0], hi[0]) && p[1].between?(lo[1], hi[1])

  def test_ray_stops_at_the_inner_face_of_the_wall
    edges = G.wall_body_edges([BOX])
    assert_in_delta 2900.0, G.ray_distance([3000.0, 2000.0], [1.0, 0.0], edges), 1e-9
    assert_in_delta 1900.0, G.ray_distance([3000.0, 2000.0], [0.0, -1.0], edges), 1e-9
  end

  def test_left_justified_wall_lies_left_of_its_axis
    wall = BOX.merge('justification' => 'left') # counter-clockwise: left is inside
    edges = G.wall_body_edges([wall])
    assert_in_delta 2800.0, G.ray_distance([3000.0, 2000.0], [1.0, 0.0], edges), 1e-9
  end

  def test_room_camera_stands_in_a_corner_and_looks_across_the_room
    edges = G.wall_body_edges([BOX])
    cam = G.room_camera([3000.0, 2000.0], edges, G.dominant_axis([BOX]))
    assert inside?(cam[:eye], [100, 100], [5900, 3900]), cam.inspect
    assert inside?(cam[:target], [100, 100], [5900, 3900]), cam.inspect
    # 350 mm off the corner along the diagonal: about 247 mm off each wall.
    gaps = [cam[:eye][0] - 100, 5900 - cam[:eye][0], cam[:eye][1] - 100, 3900 - cam[:eye][1]]
    assert_in_delta 350.0 / Math.sqrt(2), gaps.min, 1e-6
    assert_operator G.dist(cam[:eye], cam[:target]), :>, 3000.0
  end

  def test_light_openings_finds_window_centres_along_the_axis
    plan = { 'walls' => [BOX], 'openings' => [
      { 'id' => 'V1', 'wall' => 'EXT', 'opening_kind' => 'window', 'offset' => 7000.0, 'width' => 1200.0 },
      { 'id' => 'P1', 'wall' => 'EXT', 'opening_kind' => 'door', 'offset' => 1000.0, 'width' => 800.0 },
      { 'id' => 'P2', 'wall' => 'EXT', 'opening_kind' => 'door', 'offset' => 15000.0, 'width' => 2400.0 }
    ] }
    lights = G.light_openings(plan)
    assert_equal 2, lights.size # the 800 mm door lets no light in
    assert_in_delta 6000.0, lights[0][:at][0], 1e-9 # 7600 along the ring: 1600 up the east side
    assert_in_delta 1600.0, lights[0][:at][1], 1e-9
    assert_in_delta 2400.0, lights[1][:width], 1e-9
  end

  def test_room_camera_turns_toward_the_window
    edges = G.wall_body_edges([BOX])
    axis = G.dominant_axis([BOX])
    plain = G.room_camera([3000.0, 2000.0], edges, axis)
    # A window in the middle of the wall the plain camera has at its back.
    back_wall = plain[:eye][0] < 3000 ? [0.0, 2000.0] : [6000.0, 2000.0]
    cam = G.room_camera([3000.0, 2000.0], edges, axis, lights: [{ at: back_wall, width: 2000.0 }])
    look = G.unit(G.sub(cam[:target], cam[:eye]))
    to_window = G.unit(G.sub(back_wall, cam[:eye]))
    assert_operator G.dot(look, to_window), :>, Math.cos(38 * Math::PI / 180), cam.inspect
  end

  def test_window_behind_another_wall_does_not_count
    inner = { 'id' => 'M1', 'axis' => [[3000, 0], [3000, 4000]], 'thickness' => 150.0, 'justification' => 'center' }
    edges = G.wall_body_edges([BOX, inner])
    assert_equal 0.0, G.window_metres([1000.0, 2000.0], [1.0, 0.0], [{ at: [6000.0, 2000.0], width: 1500.0 }], edges)
    assert_in_delta 1.5, G.window_metres([4000.0, 2000.0], [1.0, 0.0], [{ at: [6000.0, 2000.0], width: 1500.0 }], edges), 1e-9
  end

  def test_room_camera_follows_a_rotated_plan
    a = 30 * Math::PI / 180
    rot = ->(p) { [(p[0] * Math.cos(a)) - (p[1] * Math.sin(a)), (p[0] * Math.sin(a)) + (p[1] * Math.cos(a))] }
    wall = BOX.merge('axis' => BOX['axis'].map(&rot))
    cam = G.room_camera(rot.call([3000.0, 2000.0]), G.wall_body_edges([wall]), G.dominant_axis([wall]))
    back = ->(p) { [(p[0] * Math.cos(-a)) - (p[1] * Math.sin(-a)), (p[0] * Math.sin(-a)) + (p[1] * Math.cos(-a))] }
    assert inside?(back.call(cam[:eye]), [100, 100], [5900, 3900]), cam.inspect
  end

  def test_a_point_outside_every_wall_gets_no_camera
    assert_nil G.room_camera([9000.0, 2000.0], G.wall_body_edges([BOX]), [1.0, 0.0])
  end

  def test_exterior_cameras_frame_the_whole_box
    min = [0.0, 0.0, -150.0]
    max = [10_000.0, 8000.0, 6000.0]
    corners = [min[0], max[0]].product([min[1], max[1]], [min[2], max[2]])
    cams = G.exterior_cameras(corners, 1600.0, 40.0)
    assert_equal %w[E1_suroeste E2_sureste E3_noreste E4_noroeste], cams.map { |c| c[:name] }
    tan_v = Math.tan(20 * Math::PI / 180)
    cams.each do |c|
      assert_in_delta 1600.0, c[:eye][2], 1e-9
      fwd = G.unit(G.sub(c[:target], c[:eye]))
      right = [fwd[1], -fwd[0]]
      corners.each do |x, y, z|
        o = G.sub([x, y], c[:eye])
        depth = G.dot(o, fwd)
        assert_operator depth, :>, 0.0
        assert_operator G.dot(o, right).abs / depth, :<=, (tan_v * 1.5) + 1e-9, c[:name]
        assert_operator (z - 1600.0).abs / depth, :<=, tan_v + 1e-9, c[:name]
      end
    end
    assert_operator cams[0][:eye][0], :<, 0.0, 'south-west of the box'
    assert_operator cams[0][:eye][1], :<, 0.0
  end

  def test_a_hip_apex_lets_the_camera_come_closer_than_its_box
    box = [0.0, 10_000.0].product([0.0, 8000.0], [0.0, 6000.0])
    hip = [0.0, 10_000.0].product([0.0, 8000.0], [0.0, 3000.0]) + [[5000.0, 4000.0, 6000.0]]
    far = G.dist(G.exterior_cameras(box, 1600.0, 40.0)[0][:eye], [5000.0, 4000.0])
    near = G.dist(G.exterior_cameras(hip, 1600.0, 40.0)[0][:eye], [5000.0, 4000.0])
    assert_operator near, :<, far * 0.9
  end

  def test_a_stair_beside_the_corner_moves_the_room_camera
    stair = [[4000.0, 2400.0], [5900.0, 2400.0], [5900.0, 3900.0], [4000.0, 3900.0]]
    edges = G.wall_body_edges([BOX]) + G.ring_edges([stair])
    cam = G.room_camera([3000.0, 2000.0], edges, G.dominant_axis([BOX]))
    refute(cam[:eye][0] > 3800 && cam[:eye][1] > 2200, "the eye stands clear of the stair: #{cam.inspect}")
  end

  def test_an_eye_behind_a_fence_moves_inside_it
    cams = [{ name: 'E1', eye: [-20_000.0, 0.0, 1600.0], target: [0.0, 0.0, 1600.0] },
            { name: 'E2', eye: [0.0, -5000.0, 1600.0], target: [0.0, 0.0, 1600.0] }]
    fence = { points: [[-9000, -3000], [-9000, 3000], [9000, 3000]], closed: false }
    moved, kept = G.clear_view(cams, [fence])
    assert_in_delta(-7500.0, moved[:eye][0], 1e-6)
    assert_equal [0.0, 1600.0], moved[:eye][1, 2]
    assert_equal cams[1], kept
  end

  def test_an_eye_inside_a_parked_car_moves_out_of_it
    cams = [{ name: 'E1', eye: [-10_000.0, 0.0, 1600.0], target: [0.0, 0.0, 1600.0] }]
    car = [[-11_000.0, -1000.0], [-8000.0, 1000.0]]
    (cam,) = G.clear_view(cams, [], [car])
    assert_in_delta(-7200.0, cam[:eye][0], 1e-6)
  end
end
