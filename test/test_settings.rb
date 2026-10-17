# frozen_string_literal: true

require_relative 'test_helper'
require 'plomada/security'

# PLOMADA_PORT moves one SketchUp to another port without touching the port
# the other SketchUp instances read from the shared preferences.
class TestSettings < Minitest::Test
  class Store
    attr_reader :data

    def initialize(data = {}) = @data = data
    def read_default(_section, key, default) = @data.fetch(key, default)
    def write_default(_section, key, value) = @data[key] = value
  end

  def test_without_the_variable_the_stored_port_wins
    s = Plomada::Settings.new(Store.new('port' => 7900), Plomada::CONFIG, {})
    assert_equal 7900, s.port
    refute s.port_from_env?
  end

  def test_the_variable_overrides_the_stored_port
    s = Plomada::Settings.new(Store.new('port' => 7883), Plomada::CONFIG, { 'PLOMADA_PORT' => '7884' })
    assert_equal 7884, s.port
    assert s.port_from_env?
    assert_equal true, s.snapshot['port_from_env']
  end

  def test_the_override_is_never_written_back
    store = Store.new('port' => 7883)
    s = Plomada::Settings.new(store, Plomada::CONFIG, { 'PLOMADA_PORT' => '7884' })
    s.update(port: 7884, allow_ruby: true)
    assert_equal 7883, store.data['port']
    assert_equal true, store.data['allow_ruby']
  end

  def test_an_out_of_range_variable_falls_back_with_a_warning
    %w[80 70000 abc].each do |raw|
      s = Plomada::Settings.new(Store.new('port' => 7883), Plomada::CONFIG, { 'PLOMADA_PORT' => raw })
      assert_equal 7883, s.port
      refute s.port_from_env?
      assert_equal 1, s.warnings.size
      assert_match(/PLOMADA_PORT/, s.warnings.first)
    end
  end
end
