# frozen_string_literal: true

require_relative 'core'

module Plomada
  # The component library's map entries, without SketchUp: which file an
  # entry names, where its cached .skp goes, and how an imported model is
  # normalised before it is cached (all mm).
  module Geometry
    module_function

    # Formats an entry's +src+ may name, as checked on SketchUp 2025 Pro with
    # DefinitionList#import: .glb, .dae and .stl import; a .gltf is packed
    # into a .glb first (Plomada::Gltf). .obj and .fbx have no importer
    # ("No importer found for file extension") and are refused by name with
    # the rest; convert them to .glb first.
    LIBRARY_SOURCE_EXTS = %w[.glb .gltf .dae .stl].freeze
    # Which way a source model's front faces after import -> degrees CCW
    # that turn it to -y, the SketchUp Front view the fit modes expect.
    LIBRARY_FRONTS = { '-y' => 0.0, '+x' => 270.0, '+y' => 180.0, '-x' => 90.0 }.freeze
    # Which imported axis is the model's up -> [rotation axis, degrees] that
    # turn it to +z (a model exported lying down).
    LIBRARY_UPS = { '+z' => [[0, 0, 1], 0.0], '+y' => [[1, 0, 0], 90.0], '-y' => [[1, 0, 0], -90.0],
                    '+x' => [[0, 1, 0], -90.0], '-x' => [[0, 1, 0], 90.0], '-z' => [[1, 0, 0], 180.0] }.freeze
    LIBRARY_SANE_MM = [50.0, 40_000.0].freeze # a model outside this size was probably imported in the wrong unit

    # {skp:, src:} absolute paths for a map entry under +dir+, or nil when it
    # names neither. +skp+ is the .skp Plomada loads: the entry's own "skp", or
    # for an entry with a "src" (a .glb, .dae, .obj...) the cache next to it,
    # "skp" when it names a .skp, else the source with the .skp extension.
    def library_paths(dir, value)
      return nil unless value.is_a?(Hash)

      skp = value['skp'].is_a?(String) && !value['skp'].strip.empty? ? File.expand_path(value['skp'], dir) : nil
      src = value['src'].is_a?(String) && !value['src'].strip.empty? ? File.expand_path(value['src'], dir) : nil
      return nil if skp.nil? && src.nil?
      return { skp: skp, src: nil } if src.nil?
      return nil if skp && !skp.downcase.end_with?('.skp')

      { skp: skp || src.sub(/\.[^.\/\\]+\z/, '') + '.skp', src: src }
    end

    def library_source_ext?(path) = LIBRARY_SOURCE_EXTS.include?(File.extname(path.to_s).downcase)

    # True when the cached .skp must be (re)made from the source: missing, or
    # older than the source.
    def library_stale?(skp_mtime, src_mtime)
      return true if skp_mtime.nil?
      return false if src_mtime.nil?

      skp_mtime < src_mtime
    end

    # Degrees CCW that bring a model whose front faces +front+ to face -y.
    def library_front_turn(front)
      key = (front || '-y').to_s.strip.downcase
      LIBRARY_FRONTS.fetch(key) do
        raise InvalidParams, "library front must be one of #{LIBRARY_FRONTS.keys.join(', ')}, got #{front.inspect}"
      end
    end

    # [axis, degrees] that stand a model whose up is +up+ upright.
    def library_up_rotation(up)
      key = (up || '+z').to_s.strip.downcase
      key = "+#{key}" if key.size == 1
      LIBRARY_UPS.fetch(key) do
        raise InvalidParams, "library up must be one of #{LIBRARY_UPS.keys.join(', ')}, got #{up.inspect}"
      end
    end

    # True when +name+ (an imported node's instance or definition name)
    # contains one of the "drop" patterns of a map entry, ignoring case.
    def library_drop?(name, patterns)
      n = name.to_s.downcase
      !n.empty? && Array(patterns).any? { |pat| !pat.to_s.empty? && n.include?(pat.to_s.downcase) }
    end

    # How an imported model is normalised before it is cached: scaled by
    # +scale+, turned +turn+ degrees CCW about z, then moved so the centre of
    # the bottom of its box is the origin. +bmin+, +bmax+ in mm, as imported.
    # Returns {scale:, turn_deg:, translate: [x, y, z], size: [w, d, h]} to
    # apply as T(translate) * R(turn_deg) * S(scale).
    def library_normalize(bmin, bmax, turn: 0.0, scale: 1.0)
      s = scale.to_f
      raise InvalidParams, "library src_scale must be greater than 0, got #{scale.inspect}" unless s.positive?

      size = [0, 1, 2].map { |k| (bmax[k] - bmin[k]) * s }
      raise InvalidParams, 'the imported model is empty' if size.any?(&:negative?) || size.max <= 0

      a = turn.to_f * Math::PI / 180.0
      cx = (bmin[0] + bmax[0]) / 2.0 * s
      cy = (bmin[1] + bmax[1]) / 2.0 * s
      rx = (cx * Math.cos(a)) - (cy * Math.sin(a))
      ry = (cx * Math.sin(a)) + (cy * Math.cos(a))
      quarter = (turn.to_f % 180).zero?
      { scale: s, turn_deg: turn.to_f % 360, translate: [-rx, -ry, -bmin[2] * s],
        size: quarter ? size : [size[1], size[0], size[2]] }
    end

    # A warning when a normalised model's size suggests the wrong unit, else nil.
    def library_size_note(size)
      big = size.max
      lo, hi = LIBRARY_SANE_MM
      return nil if big.between?(lo, hi)

      "#{big.round} mm across: probably imported in the wrong unit; set src_scale in the map"
    end
  end
end
