# frozen_string_literal: true

require 'json'
require_relative 'kit'
require_relative 'library_prep'
require_relative '../geometry'

module Plomada
  module SU
    # The component library (C:\mcp\biblioteca by default): mapa_componentes.json
    # says which model replaces each AutoCAD block: a .skp, or a .glb, .dae,
    # .obj... ("src") that LibraryPrep imports and caches as a .skp next to it.
    # A definition is loaded once
    # per model (remembered by its path and part), cleaned of what a download
    # carries besides the object (hidden geometry, images, texts, dimensions,
    # guides), and placed with Geometry.fit_component.
    module Library
      SECTIONS = %w[catalogo_acadmcp designcenter sitio].freeze
      NOT_GEOMETRY = [Sketchup::Image, Sketchup::Text, Sketchup::Dimension, Sketchup::ConstructionLine,
                      Sketchup::ConstructionPoint, Sketchup::SectionPlane].freeze

      module_function

      # The parsed map, or nil when +dir+ has none (then everything stays massing).
      def map(dir)
        return nil if dir.nil? || dir.to_s.empty?

        path = File.join(dir, 'mapa_componentes.json')
        return nil unless File.file?(path)

        @maps ||= {}
        mtime = File.mtime(path)
        cached = @maps[path]
        return cached[:data] if cached && cached[:mtime] == mtime

        data = JSON.parse(File.read(path, encoding: 'UTF-8'))
        @maps[path] = { mtime: mtime, data: data }
        data
      end

      # {path:, src:, fit:, rot:, scale:, part:, z:} for +key+ in +section+
      # (following string aliases to other keys), or nil when there is no
      # usable model for it. +path+ is the .skp to load; an entry with a "src"
      # (a .glb, .dae, .obj... that SketchUp imports) has its cached .skp there
      # and +src+ set, and is imported and cached on first use when the cache
      # is missing or older than the source (library_prepare does it ahead).
      # +part+ picks one top-level group or component of a file that holds
      # several objects (its index among them); +z+ lifts it (mm, a wall-hung
      # basin).
      def entry(dir, section, key)
        data = map(dir)
        return nil unless data

        value = resolve(data, data.dig(section, key))
        paths = Geometry.library_paths(dir, value)
        return nil unless paths && paths[:skp].downcase.end_with?('.skp')

        if paths[:src]
          return nil unless File.file?(paths[:src]) || File.file?(paths[:skp])
        else
          return nil unless File.file?(paths[:skp])
        end

        { path: paths[:skp], src: paths[:src], fit: value['fit'] || 'native', rot: (value['rot'] || 0).to_f,
          scale: (value['scale'] || 1).to_f, part: value['part'], z: (value['z'] || 0).to_f, value: value }
      end

      # The entry a map value stands for: a Hash, or a string naming another
      # key in any section (followed up to four times).
      def resolve(data, value)
        seen = 0
        while value.is_a?(String) && seen < 4
          alias_key = value
          value = SECTIONS.map { |s| data.dig(s, alias_key) }.find { |v| v.is_a?(Hash) }
          seen += 1
        end
        value.is_a?(Hash) ? value : nil
      end

      # The definition for an +entry+ (Library.entry): loaded once per model
      # from its .skp, or imported from its source and cached first when the
      # .skp is missing or stale.
      def definition_for(model, entry)
        src = entry[:src]
        if src && File.file?(src)
          skp_mtime = File.file?(entry[:path]) ? File.mtime(entry[:path]) : nil
          if Geometry.library_stale?(skp_mtime, File.mtime(src))
            return definition(model, entry[:path], entry[:part]) do
              defn, = LibraryPrep.import(model, src, entry[:value])
              LibraryPrep.save(defn, entry[:path])
              defn
            end
          end
        end
        definition(model, entry[:path], entry[:part])
      end

      def definition(model, path, part = nil)
        key = part.nil? ? path : "#{path}##{part}"
        found = model.definitions.find { |d| d.get_attribute(DICT, 'library_path') == key }
        return found if found

        defn = block_given? ? yield : model.definitions.load(path)
        clean(defn)
        unless part.nil?
          parts = defn.entities.select { |e| e.is_a?(Sketchup::ComponentInstance) || e.is_a?(Sketchup::Group) }
          pick = parts[part.to_i] or raise InvalidParams, "#{File.basename(path)} has #{parts.size} parts, no part #{part}"
          defn = pick.definition
        end
        defn.set_attribute(DICT, 'library_path', key)
        defn
      end

      # Faces in a definition, nested groups and components included; counted
      # once and kept on the definition.
      def face_count(defn)
        cached = defn.get_attribute(DICT, 'face_count')
        return cached if cached

        count = ->(ents) do
          ents.grep(Sketchup::Face).size + ents.grep(Sketchup::Group).sum { |g| count.call(g.entities) } +
            ents.grep(Sketchup::ComponentInstance).sum { |i| count.call(i.definition.entities) }
        end
        n = count.call(defn.entities)
        defn.set_attribute(DICT, 'face_count', n)
        n
      end

      # Erases what is not the object itself from a freshly loaded definition.
      def clean(defn)
        junk = defn.entities.select { |e| e.hidden? || NOT_GEOMETRY.any? { |k| e.is_a?(k) } }
        defn.entities.erase_entities(junk) unless junk.empty?
      end

      # Places +entry+ for a block at +at+ (mm) turned +rotation+ degrees; for
      # fit 'footprint' +at+ is the block's back-left corner and w x d its
      # size, for 'native' +at+ is where the component's centre stands.
      def place(model, entities, entry, at, rotation, z, w: nil, d: nil)
        defn = definition_for(model, entry)
        bb = defn.bounds
        bmin = SU.to_mm(bb.min)
        bmax = SU.to_mm(bb.max)
        fit = Geometry.fit_component(bmin, bmax, entry[:fit], w: w, d: d, rot: entry[:rot], scale: entry[:scale])
        centre = Geom::Vector3d.new(-((bmin[0] + bmax[0]) / 2.0).mm, -((bmin[1] + bmax[1]) / 2.0).mm, -bmin[2].mm)
        t = Geom::Transformation.translation(Geom::Vector3d.new(at[0].mm, at[1].mm, (z + (entry[:z] || 0.0)).mm)) *
            Geom::Transformation.rotation(ORIGIN, Z_AXIS, rotation.to_f.degrees) *
            Geom::Transformation.translation(Geom::Vector3d.new(fit[:translate][0].mm, fit[:translate][1].mm, 0)) *
            Geom::Transformation.rotation(ORIGIN, Z_AXIS, fit[:rotation_deg].degrees) *
            Geom::Transformation.scaling(fit[:scale]) *
            Geom::Transformation.translation(centre)
        entities.add_instance(defn, t)
      end
    end
  end
end
