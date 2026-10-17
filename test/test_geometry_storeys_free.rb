# frozen_string_literal: true

require_relative 'test_helper'

# Free storey elevations (a split-level hotel: basement -1.50 m, ground floor
# +1.00, levels at +5.75, +9.00 and +12.25), the ground level the site and
# terrain stand on, and stair wells chosen by elevation, not build order.
class TestGeometryStoreysFree < Minitest::Test
  L = Plomada::Geometry::Levels

  LEVELS = [
    { 'name' => 'N02', 'elevation' => 9000.0, 'height' => 3100.0 },
    { 'name' => 'SS', 'elevation' => -1500.0, 'height' => 2350.0 },
    { 'name' => 'N03', 'elevation' => 12_250.0, 'height' => 3100.0 },
    { 'name' => 'N00', 'elevation' => 1000.0, 'height' => 4600.0 },
    { 'name' => 'N01', 'elevation' => 5750.0, 'height' => 3100.0 }
  ].freeze

  def test_explicit_elevations_are_kept_as_given
    [-1500.0, 1000.0, 5750.0, 9000.0, 12_250.0].each do |e|
      assert_equal e, L.elevation_for(LEVELS, 'X', e, 150.0)
    end
    assert_equal %w[SS N00 N01 N02 N03], L.sorted(LEVELS).map { |s| s['name'] }
  end

  def test_wells_come_from_the_storey_below_by_elevation_not_build_order
    assert_equal 'SS', L.below(LEVELS, 1000.0, 'N00')['name']
    assert_equal 'N00', L.below(LEVELS, 5750.0, 'N01')['name']
    assert_equal 'N01', L.below(LEVELS, 9000.0, 'N02')['name']
    assert_equal 'N02', L.below(LEVELS, 12_250.0, 'N03')['name']
    assert_nil L.below(LEVELS, -1500.0, 'SS')
    assert_equal 'N01', L.above(LEVELS, 1000.0, 'N00')['name']
    LEVELS.permutation.first(24).each do |order|
      assert_equal 'N00', L.below(order, 5750.0, 'N01')['name'], 'any build order gives the same storey below'
    end
  end

  def test_the_site_goes_on_the_storey_nearest_the_ground_level
    assert_equal 'N00', L.site_storey(LEVELS, 1000.0)
    assert_equal 'N00', L.site_storey(LEVELS, 0.0), 'N00 at +1000 is closer to 0 than SS at -1500'
    assert_equal 'SS', L.site_storey(LEVELS, -1500.0)
    assert_nil L.site_storey(LEVELS, 3600.0), 'no storey within 1500 mm'
    assert_nil L.site_storey([], 0.0)
  end

  def test_the_site_stands_on_the_slab_underside_or_on_the_ground_level
    assert_equal(-150.0, L.site_ground(0.0, 150.0, 0.0, false))
    assert_equal(-150.0, L.site_ground(1000.0, 150.0, 0.0, false))
    assert_equal(-1000.0, L.site_ground(1000.0, 150.0, 0.0, true))
  end

  def test_basements_are_the_storeys_below_ground
    assert_equal %w[SS], L.basements(LEVELS, 0.0).map { |s| s['name'] }
    assert_empty L.basements(LEVELS, -2000.0)
  end

  def test_auto_stacking_is_the_0_4_rule
    assert_equal 0.0, L.elevation_for([], 'N00', nil, 150.0)
    two = [{ 'name' => 'N00', 'elevation' => 0.0, 'height' => 2800.0 }]
    assert_equal 2950.0, L.elevation_for(two, 'N01', nil, 150.0)
    assert_equal 0.0, L.elevation_for(two, 'N00', nil, 150.0), 'a rebuilt storey keeps its elevation'
  end

  def test_floor_to_floor_heights_leave_the_walls_under_the_next_slab
    assert_equal 4600.0, L.wall_height(4750.0, 150.0)
    err = assert_raises(Plomada::InvalidParams) { L.wall_height(100.0, 150.0) }
    assert_match(/storey height 100 mm floor to floor leaves no wall under a 150 mm slab/, err.message)
  end

  def test_storey_records_take_an_explicit_elevation_in_range
    walls = [{ 'id' => 'A', 'axis' => [[0, 0], [1000, 0]], 'thickness' => 200 }]
    plan = Plomada::Plan.normalize('walls' => walls, 'storey' => { 'name' => 'SS', 'height' => 3000,
                                                                     'elevation_mode' => 'explicit', 'elevation' => -1500 })
    assert_equal({ 'name' => 'SS', 'height' => 3000.0, 'elevation_mode' => 'explicit', 'elevation' => -1500.0 }, plan['storey'])
    err = assert_raises(Plomada::InvalidParams) do
      Plomada::Plan.normalize('walls' => walls, 'storey' => { 'elevation_mode' => 'explicit', 'elevation' => 70_000 })
    end
    assert_equal 'storey.elevation must be between -10000 and 60000 mm, got 70000', err.message
    err = assert_raises(Plomada::InvalidParams) do
      Plomada::Plan.normalize('walls' => walls, 'storey' => { 'elevation_mode' => 'explicit' })
    end
    assert_equal 'storey.elevation is required when storey.elevation_mode is explicit', err.message
    auto = Plomada::Plan.normalize('walls' => walls)
    assert_equal({ 'name' => 'N00', 'height' => 2800.0 }, auto['storey'], 'auto storeys normalize as in 0.4')
  end
end
