# frozen_string_literal: true

require 'json'
require 'base64'
require 'uri'
require_relative 'errors'

module Plomada
  # Packs a .gltf (JSON with its .bin buffers and texture images in separate
  # files, as Poly Haven ships them) into one binary .glb, the only glTF form
  # SketchUp 2025 imports. Pure Ruby, no SketchUp: every buffer becomes one
  # BIN chunk, every image file a bufferView in it; nothing else changes.
  module Gltf
    MAGIC = 0x46546C67   # "glTF"
    JSON_CHUNK = 0x4E4F534A
    BIN_CHUNK = 0x004E4942
    MIME = { '.png' => 'image/png', '.jpg' => 'image/jpeg', '.jpeg' => 'image/jpeg', '.webp' => 'image/webp',
             '.ktx2' => 'image/ktx2' }.freeze

    module_function

    # The .glb bytes for the .gltf at +path+.
    def pack_file(path)
      json = JSON.parse(File.read(path, encoding: 'UTF-8'))
      pack(json, File.dirname(path))
    end

    # The .glb bytes for parsed glTF +json+ whose relative URIs resolve in +dir+.
    def pack(json, dir)
      gltf = JSON.parse(JSON.generate(json)) # a deep copy we may rewrite
      bin = +''.b
      offsets = (gltf['buffers'] || []).map do |buf|
        pad!(bin)
        start = bin.bytesize
        data = read_uri(buf['uri'], dir, "buffer #{buf['uri'].inspect}")
        raise InvalidParams, "glTF buffer #{buf['uri'].inspect} is shorter than its byteLength" if data.bytesize < buf['byteLength'].to_i

        bin << data.byteslice(0, buf['byteLength'].to_i)
        start
      end
      views = gltf['bufferViews'] || []
      views.each { |v| v['byteOffset'] = v.fetch('byteOffset', 0) + offsets.fetch(v['buffer'], 0); v['buffer'] = 0 }
      (gltf['images'] || []).each do |img|
        next unless img['uri']

        data = read_uri(img['uri'], dir, "image #{img['uri'].inspect}")
        pad!(bin)
        views << { 'buffer' => 0, 'byteOffset' => bin.bytesize, 'byteLength' => data.bytesize }
        bin << data
        img['mimeType'] ||= MIME.fetch(File.extname(URI.decode_www_form_component(img['uri'].split('?').first)).downcase,
                                       'image/png')
        img.delete('uri')
        img['bufferView'] = views.size - 1
      end
      gltf['bufferViews'] = views unless views.empty?
      pad!(bin)
      gltf['buffers'] = bin.empty? ? [] : [{ 'byteLength' => bin.bytesize }]
      gltf.delete('buffers') if gltf['buffers'].empty?
      glb(JSON.generate(gltf), bin)
    end

    # The bytes a data: URI or a relative file URI names.
    def read_uri(uri, dir, what)
      raise InvalidParams, "glTF #{what} has no uri" unless uri.is_a?(String) && !uri.empty?

      if uri.start_with?('data:')
        head, _, body = uri.partition(',')
        return head.end_with?(';base64') ? Base64.decode64(body) : URI.decode_www_form_component(body).b
      end
      raise InvalidParams, "glTF #{what} is remote; download it next to the .gltf first" if uri =~ %r{\A[a-z][a-z0-9+.-]*://}i

      file = File.expand_path(URI.decode_www_form_component(uri), dir)
      raise InvalidParams, "glTF #{what}: no file at #{file}" unless File.file?(file)

      File.binread(file)
    end

    def pad!(bin, byte = "\0")
      bin << (byte * ((4 - (bin.bytesize % 4)) % 4)).b
    end

    # A GLB 2.0 container: header, JSON chunk (space padded), BIN chunk.
    def glb(json_text, bin)
      js = json_text.b
      js << (' ' * ((4 - (js.bytesize % 4)) % 4)).b
      chunks = [js.bytesize, JSON_CHUNK].pack('V2') + js
      chunks << [bin.bytesize, BIN_CHUNK].pack('V2') << bin unless bin.empty?
      [MAGIC, 2, 12 + chunks.bytesize].pack('V3') + chunks
    end

    # [json, bin] of a .glb, for tests and checks.
    def unpack(bytes)
      magic, version, length = bytes.unpack('V3')
      raise InvalidParams, 'not a GLB 2.0 file' unless magic == MAGIC && version == 2 && length == bytes.bytesize

      pos = 12
      json = nil
      bin = nil
      while pos < length
        size, type = bytes.byteslice(pos, 8).unpack('V2')
        data = bytes.byteslice(pos + 8, size)
        json = JSON.parse(data) if type == JSON_CHUNK
        bin = data if type == BIN_CHUNK
        pos += 8 + size
      end
      [json, bin]
    end
  end
end
