# frozen_string_literal: true

require 'json'
require_relative '../config'
require_relative '../errors'

module Plomada
  # Everything below Plomada::SU talks to the SketchUp API. Lengths cross the
  # boundary here: plans and geometry are millimetre floats, SketchUp gets
  # Numeric#mm lengths and gives back Length values that #to_mm converts.
  module SU
    DICT = CONFIG[:dictionary]

    module_function

    def pt(p) = Geom::Point3d.new(p[0].mm, p[1].mm, (p[2] || 0.0).mm)
    def vec(v) = Geom::Vector3d.new(v[0], v[1], v[2] || 0.0)
    def mm(len) = len.to_f * 25.4
    def to_mm(point) = [mm(point.x), mm(point.y), mm(point.z)]

    def bounds_mm(bb)
      return nil if bb.empty?

      { 'min' => to_mm(bb.min).map { |v| v.round(1) }, 'max' => to_mm(bb.max).map { |v| v.round(1) },
        'size' => [mm(bb.width), mm(bb.height), mm(bb.depth)].map { |v| v.round(1) } }
    end

    def entities_build? = Sketchup::Entities.method_defined?(:build)
    def pbr? = Sketchup::Material.method_defined?(:metallic_factor=) || Sketchup::Material.method_defined?(:metalness=)

    def capabilities
      exporters = Sketchup.find_support_file('Exporters')
      fbx = exporters && !Dir.glob(File.join(exporters.tr('\\', '/'), 'skp2fbx*')).empty?
      { 'pro' => Sketchup.is_pro?, 'entities_build' => entities_build?, 'pbr' => pbr?, 'fbx_export' => fbx ? true : false }
    end

    # --- tags and materials ---------------------------------------------------------

    def ensure_tags(model)
      CONFIG[:tags].each { |name| tag(model, name) }
    end

    def tag(model, name)
      model.layers[name] || model.layers.add(name)
    end

    # Finds or creates a material and sets it to exactly these values; when
    # the PBR API exists, MAT_metal gets metalness and MAT_vidrio roughness.
    def ensure_material(model, name, rgb, alpha)
      mat = model.materials[name] || model.materials.add(name)
      mat.color = Sketchup::Color.new(*rgb)
      mat.alpha = alpha.to_f
      pbr = CONFIG[:pbr][name]
      apply_pbr(mat, pbr) if pbr && pbr?
      mat
    end

    def apply_pbr(mat, pbr)
      if pbr[:metallic]
        if mat.respond_to?(:metallic_factor=)
          mat.metalness_enabled = true if mat.respond_to?(:metalness_enabled=)
          mat.metallic_factor = pbr[:metallic]
        elsif mat.respond_to?(:metalness=)
          mat.metalness = pbr[:metallic]
        end
      end
      return unless pbr[:roughness]

      if mat.respond_to?(:roughness_factor=)
        mat.roughness_enabled = true if mat.respond_to?(:roughness_enabled=)
        mat.roughness_factor = pbr[:roughness]
      elsif mat.respond_to?(:roughness=)
        mat.roughness = pbr[:roughness]
      end
    end

    def ensure_materials(model)
      CONFIG[:materials].each { |name, (rgb, alpha)| ensure_material(model, name, rgb, alpha) }
    end

    def material(model, name)
      known = CONFIG[:materials][name]
      return ensure_material(model, name, known[0], known[1]) if known

      model.materials[name] || raise(InvalidParams, "material #{name.inspect} is not in the model; Plomada materials are " \
                                                    "#{CONFIG[:materials].keys.join(', ')}")
    end

    # --- attributes -----------------------------------------------------------------

    def plomada?(entity)
      entity.valid? && !entity.attribute_dictionary(DICT).nil?
    end

    def kind(entity) = entity.get_attribute(DICT, 'kind')

    def set_attrs(entity, hash)
      hash.each { |k, v| entity.set_attribute(DICT, k.to_s, v) }
      entity
    end

    def record(entity)
      raw = entity.get_attribute(DICT, 'record')
      raw ? JSON.parse(raw) : nil
    end

    # Top-level groups and component instances carrying a plomada attribute.
    def plomada_entities(model)
      model.entities.select do |e|
        (e.is_a?(Sketchup::Group) || e.is_a?(Sketchup::ComponentInstance)) && plomada?(e)
      end
    end

    def find_group(model, name)
      model.entities.grep(Sketchup::Group).find { |g| g.valid? && g.name == name && plomada?(g) }
    end

    # The opening component +id+, on +storey+ when one is named (else the first).
    def find_opening(model, id, storey = nil) = find_openings(model, id, storey).first

    # Every component of opening +id+: one, or one per panel on a curved wall.
    def find_openings(model, id, storey = nil)
      model.entities.grep(Sketchup::ComponentInstance).select do |i|
        i.valid? && kind(i) == 'opening' && i.get_attribute(DICT, 'id') == id &&
          (storey.nil? || (i.get_attribute(DICT, 'storey') || CONFIG[:storey_prefix]) == storey)
      end
    end

    def group_name(storey, suffix) = "#{storey}_#{suffix}"

    # Faces of an entities collection from plain {outer:, holes:, normal:}
    # faces, through Entities#build when the step makes more than
    # builder_min_faces faces (and the API exists), else Entities#add_face.
    # Walls pass always_build: their faces are cut from one grid and must not
    # be merged or split by SketchUp, whatever their count. Every face is
    # checked against its intended normal and reversed if SketchUp oriented
    # it the other way.
    def add_faces(entities, faces, always_build: false)
      created = []
      use_builder = (always_build || faces.size > CONFIG[:builder_min_faces]) && entities.respond_to?(:build)
      if use_builder
        entities.build do |b|
          faces.each do |f|
            outer = f[:outer].map { |p| pt(p) }
            holes = (f[:holes] || []).map { |h| h.map { |p| pt(p) } }
            face = holes.empty? ? b.add_face(outer) : b.add_face(outer, holes: holes)
            created << [face, f]
          end
        end
      else
        faces.each do |f|
          face = entities.add_face(f[:outer].map { |p| pt(p) })
          (f[:holes] || []).each do |h|
            inner = entities.add_face(h.map { |p| pt(p) })
            inner.erase! if inner&.valid?
          end
          created << [face, f]
        end
      end
      created.each do |face, f|
        next unless face&.valid? && f[:normal]

        face.reverse! if face.normal.dot(vec(f[:normal])).negative?
      end
      created
    end

    # Softens and smooths every edge between two faces whose normals differ
    # by less than smooth_max_angle_deg: the facets of a tessellated curve
    # (slab edges, bands, columns, pools, railings, ramps) shade as one
    # surface while real corners stay drawn. Returns how many it softened.
    def soften(entities, max_deg = CONFIG[:smooth_max_angle_deg])
      limit = Math.cos(max_deg * Math::PI / 180.0)
      count = 0
      entities.grep(Sketchup::Edge).each do |e|
        faces = e.faces
        next unless faces.size == 2 && faces[0].normal.dot(faces[1].normal) > limit

        e.soft = true
        e.smooth = true
        count += 1
      end
      entities.grep(Sketchup::Group).each { |g| count += soften(g.entities, max_deg) }
      count
    end

    def faces_from_loops(loops)
      loops.map { |pts| { outer: pts, holes: [], normal: Geometry.unit3(Geometry.newell(pts)) } }
    end

    def group_on(model, parent_entities, name, tag_name, material_name)
      g = parent_entities.add_group
      g.name = name
      g.layer = tag(model, tag_name) if tag_name
      g.material = material(model, material_name) if material_name
      g
    end
  end
end
