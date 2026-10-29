# frozen_string_literal: true

require 'fileutils'
require 'tmpdir'
require_relative 'kit'
require_relative '../geometry'
require_relative '../jobs'
require_relative '../plan'
require_relative '../gltf'

module Plomada
  module SU
    # Library models that are not .skp: an entry of mapa_componentes.json with
    # "src" (a .glb, .gltf, .dae or .stl) is imported with
    # DefinitionList#import (a .gltf packed into a temporary .glb first),
    # normalised (unit scale, front turned to -y, the centre of its bottom on
    # the origin, its materials prefixed) and saved as a .skp cache next to
    # the source, which Library then loads like any .skp.
    #
    # library_prepare makes the caches ahead, without touching the user's
    # model: each import runs inside an operation that is aborted after the
    # .skp is written, and whatever survives the abort is removed by hand.
    # A build that meets a source without a cache imports it on first use.
    module LibraryPrep
      # Importer options per extension: no summary dialog, nothing merged.
      IMPORT_OPTIONS = {
        '.dae' => { 'merge_coplanar_faces' => false, 'show_summary' => false },
        '.stl' => { 'merge_coplanar_faces' => false, 'show_summary' => false, 'swap_yz' => false }
      }.freeze

      module_function

      # Imports +src+ into +model+ as a new definition and normalises it from
      # the map entry +value+: "drop" (names of imported nodes to leave out, a
      # ground plane), "up" (the imported axis that is up, for a model lying
      # down), "src_scale" and "front". Returns [definition, info].
      def import(model, src, value)
        value ||= {}
        unless Geometry.library_source_ext?(src)
          raise InvalidParams, "#{File.basename(src)}: SketchUp cannot import #{File.extname(src).inspect}; use " \
                               "#{Geometry::LIBRARY_SOURCE_EXTS.join(', ')} (convert .obj, .fbx or .blend to .glb first)"
        end
        raise InvalidParams, "library source not found: #{src}" unless File.file?(src)

        turn = Geometry.library_front_turn(value['front'])
        axis, tilt = Geometry.library_up_rotation(value['up'])
        mats_before = model.materials.map(&:name)
        defn = import_file(model, src)
        unless defn.is_a?(Sketchup::ComponentDefinition)
          raise Error.new(Codes::SKETCHUP, "SketchUp could not import #{File.basename(src)}")
        end

        Library.clean(defn)
        dropped = drop(defn, value['drop'])
        unless tilt.zero?
          defn.entities.transform_entities(Geom::Transformation.rotation(ORIGIN, Geom::Vector3d.new(*axis), tilt.degrees),
                                           defn.entities.to_a)
        end
        bb = defn.bounds
        raise Error.new(Codes::SKETCHUP, "#{File.basename(src)} imported empty") if bb.empty?

        norm = Geometry.library_normalize(SU.to_mm(bb.min), SU.to_mm(bb.max), turn: turn, scale: value['src_scale'] || 1)
        t = Geom::Transformation.translation(Geom::Vector3d.new(*norm[:translate].map(&:mm))) *
            Geom::Transformation.rotation(ORIGIN, Z_AXIS, norm[:turn_deg].degrees) *
            Geom::Transformation.scaling(norm[:scale])
        defn.entities.transform_entities(t, defn.entities.to_a) unless t.identity?
        stem = File.basename(src, '.*')
        new_mats = model.materials.reject { |m| mats_before.include?(m.name) }
        new_mats.each do |m|
          m.name = model.materials.unique_name("lib_#{stem}_#{m.name}") unless m.name.start_with?("lib_#{stem}_")
        end
        defn.name = model.definitions.unique_name("lib_#{stem}")
        defn.description = "Plomada library: #{File.basename(src)}"
        info = { 'faces' => Library.face_count(defn), 'size_mm' => norm[:size].map { |v| v.round(1) },
                 'materials' => new_mats.size, 'textures' => new_mats.count(&:texture) }
        info['dropped'] = dropped unless dropped.zero?
        note = Geometry.library_size_note(norm[:size])
        info['warning'] = note if note
        [defn, info]
      end

      # Erases, at any depth, the groups and components whose name or
      # definition name matches a "drop" pattern. Returns how many.
      def drop(defn, patterns, seen = {})
        return 0 if patterns.nil? || Array(patterns).empty? || seen[defn]

        seen[defn] = true
        doomed = defn.entities.select do |e|
          (e.is_a?(Sketchup::ComponentInstance) || e.is_a?(Sketchup::Group)) &&
            (Geometry.library_drop?(e.name, patterns) || Geometry.library_drop?(e.definition.name, patterns))
        end
        defn.entities.erase_entities(doomed) unless doomed.empty?
        doomed.size + defn.entities.select { |e| e.respond_to?(:definition) }.sum { |e| drop(e.definition, patterns, seen) }
      end

      # DefinitionList#import of +src+; a .gltf goes through a packed .glb.
      def import_file(model, src)
        ext = File.extname(src).downcase
        return model.definitions.import(src, IMPORT_OPTIONS.fetch(ext, {}).transform_keys(&:to_sym)) unless ext == '.gltf'

        tmp = File.join(Dir.tmpdir, "plomada_#{Process.pid}_#{File.basename(src, '.*')}.glb")
        File.binwrite(tmp, Gltf.pack_file(src))
        begin
          model.definitions.import(tmp, {})
        ensure
          File.delete(tmp) if File.file?(tmp)
        end
      end

      # Writes +defn+ to +path+ (.skp) and checks the file is there.
      def save(defn, path)
        FileUtils.mkdir_p(File.dirname(path))
        ok = defn.save_as(path)
        raise Error.new(Codes::SKETCHUP, "SketchUp could not write #{path}") unless ok && File.file?(path)

        path
      end

      # Imports +src+, saves the cache +skp+ and leaves +model+ as it was.
      def cache(model, src, skp, value)
        defs_before = model.definitions.map(&:entityID)
        mats_before = model.materials.map(&:name)
        raise Error.new(Codes::SKETCHUP, 'SketchUp refused to start the library import') unless
          model.start_operation('Plomada: library prepare', true)

        begin
          defn, info = import(model, src, value)
          save(defn, skp)
          info.merge('bytes' => File.size(skp))
        ensure
          model.abort_operation
          leftovers(model, defs_before, mats_before)
        end
      end

      # Removes what an aborted import may leave behind: definitions and
      # materials that were not in the model before it.
      def leftovers(model, defs_before, mats_before)
        model.definitions.reject { |d| defs_before.include?(d.entityID) }.each do |d|
          model.definitions.remove(d) if d.valid? && d.instances.empty?
        end
        model.materials.reject { |m| mats_before.include?(m.name) }.each { |m| model.materials.remove(m) if m.valid? }
      end

      # {section:, key:, value:, skp:, src:} for every map entry with a src,
      # or those named by +keys+ (a key with no src is refused by name).
      def sources(data, dir, keys)
        all = Library::SECTIONS.flat_map do |section|
          (data[section] || {}).filter_map do |key, value|
            paths = Geometry.library_paths(dir, value)
            next unless paths && paths[:src]

            { section: section, key: key, value: value, skp: paths[:skp], src: paths[:src] }
          end
        end
        return all if keys.nil?

        missing = keys - all.map { |e| e[:key] }
        raise InvalidParams, "no library entry with a src for: #{missing.join(', ')}" unless missing.empty?

        all.select { |e| keys.include?(e[:key]) }
      end

      # The library_prepare job: one step per source file (entries that share
      # one are converted once), no undo entry, the user's model untouched.
      def job(params, ctx)
        dir = Plan.text(params['library'], 'library')
        data = Library.map(dir) or raise InvalidParams, "no mapa_componentes.json in #{dir}"
        keys = params['keys'].nil? ? nil : Array(params['keys']).map { |k| Plan.text(k, 'keys[]') }
        force = Plan.bool(params, 'force', false, 'params')
        entries = sources(data, dir, keys)
        done = {}
        results = []
        units = entries.group_by { |e| e[:skp] }.map do |skp, group|
          ["prepare #{group.map { |e| e[:key] }.join(', ')}", lambda {
            res = done[skp] ||= prepare(ctx.model, group.first, force)
            group.each { |e| results << { 'section' => e[:section], 'key' => e[:key] }.merge(res) }
          }]
        end
        units = [['nothing to prepare', -> {}]] if units.empty?
        UnitJob.new('Plomada: library prepare', units, operation: false) do
          counts = results.map { |r| r['status'] }.tally
          { 'library' => dir, 'entries' => results, 'converted' => counts.fetch('converted', 0),
            'cached' => counts.fetch('cached', 0), 'failed' => results.size - counts.fetch('converted', 0) -
              counts.fetch('cached', 0) }
        end
      end

      def prepare(model, entry, force)
        src = entry[:src]
        skp = entry[:skp]
        base = { 'src' => src, 'skp' => skp }
        return base.merge('status' => 'missing_src', 'error' => "no file at #{src}") unless File.file?(src)

        skp_mtime = File.file?(skp) ? File.mtime(skp) : nil
        if !force && !Geometry.library_stale?(skp_mtime, File.mtime(src))
          return base.merge('status' => 'cached', 'bytes' => File.size(skp))
        end

        base.merge('status' => 'converted').merge(cache(model, src, skp, entry[:value]))
      rescue StandardError => e
        base.merge('status' => 'failed', 'error' => e.message)
      end
    end
  end
end
