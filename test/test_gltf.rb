# frozen_string_literal: true

require_relative 'test_helper'
require 'tmpdir'
require 'plomada/gltf'

# Packing a .gltf with its .bin and textures into the .glb SketchUp imports.
class TestGltf < Minitest::Test
  PNG = "\x89PNG\r\n\x1A\nfake".b

  def write_gltf(dir)
    File.binwrite(File.join(dir, 'mesh.bin'), (0...36).to_a.pack('C*'))
    Dir.mkdir(File.join(dir, 'textures'))
    File.binwrite(File.join(dir, 'textures', 'wood diff.jpg'), 'JPEGDATA'.b)
    gltf = {
      'asset' => { 'version' => '2.0' },
      'buffers' => [{ 'uri' => 'mesh.bin', 'byteLength' => 36 },
                    { 'uri' => "data:application/octet-stream;base64,#{Base64.strict_encode64('ABCDE')}",
                      'byteLength' => 5 }],
      'bufferViews' => [{ 'buffer' => 0, 'byteOffset' => 12, 'byteLength' => 24 },
                        { 'buffer' => 1, 'byteLength' => 5 }],
      'images' => [{ 'uri' => 'textures/wood%20diff.jpg' },
                   { 'uri' => "data:image/png;base64,#{Base64.strict_encode64(PNG)}" }]
    }
    path = File.join(dir, 'model.gltf')
    File.write(path, JSON.generate(gltf))
    path
  end

  def test_buffers_and_images_land_in_one_bin_chunk_at_the_right_offsets
    Dir.mktmpdir do |dir|
      bytes = Plomada::Gltf.pack_file(write_gltf(dir))
      assert_equal 0, bytes.bytesize % 4
      json, bin = Plomada::Gltf.unpack(bytes)
      assert_equal [{ 'byteLength' => bin.bytesize }], json['buffers']
      view = ->(i) { v = json['bufferViews'][i]; bin.byteslice(v['byteOffset'], v['byteLength']) }
      assert_equal (12...36).to_a.pack('C*'), view.call(0)
      assert_equal 'ABCDE', view.call(1)
      assert_equal %w[image/jpeg image/png], json['images'].map { |i| i['mimeType'] }
      assert(json['images'].none? { |i| i.key?('uri') })
      assert_equal 'JPEGDATA', view.call(json['images'][0]['bufferView'])
      assert_equal PNG, view.call(json['images'][1]['bufferView'])
      assert(json['bufferViews'].all? { |v| v['buffer'].zero? && (v['byteOffset'] % 4).zero? })
    end
  end

  def test_a_missing_file_or_a_remote_uri_is_refused_by_name
    Dir.mktmpdir do |dir|
      path = write_gltf(dir)
      File.delete(File.join(dir, 'mesh.bin'))
      err = assert_raises(Plomada::InvalidParams) { Plomada::Gltf.pack_file(path) }
      assert_match(/buffer "mesh.bin": no file at/, err.message)
      remote = { 'asset' => {}, 'buffers' => [{ 'uri' => 'https://x.org/a.bin', 'byteLength' => 1 }] }
      err = assert_raises(Plomada::InvalidParams) { Plomada::Gltf.pack(remote, dir) }
      assert_match(/is remote/, err.message)
    end
  end

  def test_a_gltf_without_buffers_packs_to_a_json_only_glb
    json, bin = Plomada::Gltf.unpack(Plomada::Gltf.pack({ 'asset' => { 'version' => '2.0' } }, '.'))
    assert_equal({ 'asset' => { 'version' => '2.0' } }, json)
    assert_nil bin
  end
end
