# frozen_string_literal: true

require_relative 'errors'
require_relative 'config'
require_relative 'geometry/arc'

module Plomada
  # Validates and normalizes plan JSON (walls, openings, rooms) into plain
  # hashes with string keys and Float millimetres. Every refusal is an
  # InvalidParams whose message starts with the field path, the same wording
  # the bridge produces from its pydantic models.
  #
  # Polyline vertices (wall axes, outlines, holes, paths) are [x, y] or
  # [x, y, bulge] (DXF convention, see Geometry::Arc); a zero bulge is
  # dropped, so a plan without arcs normalizes exactly as in 0.4.
  module Plan
    JUSTIFICATIONS = %w[center left right].freeze
    OPENING_KINDS = %w[door window].freeze
    SWINGS = %w[in out].freeze
    HANDS = %w[left right].freeze
    STAIR_KINDS = %w[straight l u].freeze
    TURNS = %w[left right].freeze
    HEADS = %w[flat arch].freeze
    ELEVATION_MODES = %w[auto explicit].freeze
    RAILING_STYLES = %w[glass bars solid].freeze
    RECORD_VERSION = 1
    MAX_LENGTH_MM = 1_000_000.0 # 1 km: anything larger is a units mistake
    # The 0.5 record lists, in build order, with their singular kind.
    EXTRA_KINDS = { 'slabs' => 'slab', 'columns' => 'column', 'curtain_walls' => 'curtain_wall',
                    'railings' => 'railing', 'planters' => 'planter', 'pools' => 'pool', 'ramps' => 'ramp' }.freeze

    module_function

    # Returns {'walls' => [...], 'openings' => [...], 'rooms' => [...], 'stairs' => [...], 'furniture' => [...],
    # 'site' => {...}, 'storey' => {...}, 'site_on_ground' => bool} plus the 0.5 lists of EXTRA_KINDS.
    def normalize(raw, config = CONFIG)
      raise InvalidParams, "plan must be an object, got #{type_name(raw)}" unless raw.is_a?(Hash)

      storey = normalize_storey(raw['storey'], config)
      walls = list(raw, 'walls').each_with_index.map { |w, i| wall(w, "walls[#{i}]") }
      raise InvalidParams, 'walls must hold at least one wall' if walls.empty?

      unique!(walls, 'walls')
      by_id = walls.to_h { |w| [w['id'], w] }
      openings = list(raw, 'openings').each_with_index.map do |o, i|
        opening(o, "openings[#{i}]", by_id, config)
      end
      unique!(openings, 'openings')
      rooms = list(raw, 'rooms').each_with_index.map { |r, i| room(r, "rooms[#{i}]") }
      unique!(rooms, 'rooms')
      stairs = list(raw, 'stairs').each_with_index.map { |st, i| stair(st, "stairs[#{i}]") }
      unique!(stairs, 'stairs')
      furniture = list(raw, 'furniture').each_with_index.map { |f, i| furniture(f, "furniture[#{i}]") }
      unique!(furniture, 'furniture')
      out = { 'walls' => walls, 'openings' => openings, 'rooms' => rooms, 'stairs' => stairs, 'furniture' => furniture,
              'site' => site(raw['site']), 'storey' => storey,
              'site_on_ground' => bool(raw, 'site_on_ground', false, 'site_on_ground') }
      EXTRA_KINDS.each do |key, kind|
        out[key] = list(raw, key).each_with_index.map { |r, i| send(kind, r, "#{key}[#{i}]", config) }
        unique!(out[key], key)
      end
      slab_ids = out['slabs'].map { |s| s['id'] }
      %w[railings planters pools].each do |key|
        out[key].each_with_index do |r, i|
          next if r['on'].nil? || slab_ids.include?(r['on'])

          raise InvalidParams, "#{key}[#{i}].on names slab #{r['on'].inspect}, which is not in the plan; " \
                               "slabs are #{slab_ids.empty? ? 'none' : slab_ids.join(', ')}"
        end
      end
      out
    end

    def normalize_storey(raw, config)
      raw ||= {}
      raise InvalidParams, "storey must be an object, got #{type_name(raw)}" unless raw.is_a?(Hash)

      height = raw.key?('height') ? positive(raw['height'], 'storey.height') : config[:storey_height_mm]
      name = raw.key?('name') ? text(raw['name'], 'storey.name') : config[:storey_prefix]
      out = { 'name' => name, 'height' => height }
      mode = choice(raw['elevation_mode'] || 'auto', 'storey.elevation_mode', ELEVATION_MODES)
      if mode == 'explicit' || !raw['elevation'].nil?
        if raw['elevation'].nil?
          raise InvalidParams, 'storey.elevation is required when storey.elevation_mode is explicit'
        end

        out['elevation_mode'] = 'explicit'
        out['elevation'] = elevation(raw['elevation'], 'storey.elevation', config)
      end
      out
    end

    def elevation(value, path, config = CONFIG)
      f = number(value, path)
      lo = config[:elevation_min_mm]
      hi = config[:elevation_max_mm]
      raise InvalidParams, "#{path} must be between #{fmt(lo)} and #{fmt(hi)} mm, got #{fmt(f)}" unless f.between?(lo, hi)

      f
    end

    def wall(raw, path)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      closed = raw.fetch('closed', false)
      raise InvalidParams, "#{path}.closed must be true or false, got #{closed.inspect}" unless [true, false].include?(closed)

      axis_raw = raw['axis']
      raise InvalidParams, "#{path}.axis must be a list of [x, y] points, got #{type_name(axis_raw)}" unless axis_raw.is_a?(Array)

      axis = vertices(axis_raw, "#{path}.axis", "wall #{id}", closed, 0)
      need = closed ? 3 : 2
      need = 2 if closed && axis.size == 2 && axis.any? { |v| v.size > 2 } # two arcs close a ring
      if axis.size < need
        raise InvalidParams, "#{path}.axis must hold at least #{need} points for a #{closed ? 'closed' : 'open'} wall, got #{axis.size}"
      end

      (1...axis.size).each do |i|
        if dist(axis[i - 1], axis[i]) <= 1e-9
          raise InvalidParams, "#{path}.axis[#{i}] repeats axis[#{i - 1}]; a zero-length segment has no direction"
        end
      end
      if closed && dist(axis[-1], axis[0]) <= 1e-9
        raise InvalidParams, "#{path}.axis[#{axis.size - 1}] repeats axis[0]; closed already closes the loop"
      end

      out = {
        'id' => id,
        'axis' => axis,
        'thickness' => positive(raw['thickness'], "#{path}.thickness"),
        'justification' => choice(raw.fetch('justification', 'center'), "#{path}.justification", JUSTIFICATIONS),
        'material' => raw.key?('material') && !raw['material'].nil? ? text(raw['material'], "#{path}.material") : 'brick',
        'closed' => closed
      }
      out['height'] = positive(raw['height'], "#{path}.height") unless raw['height'].nil?
      out
    end

    def opening(raw, path, walls_by_id, config)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      wall_id = text(raw['wall'], "#{path}.wall")
      unless walls_by_id.key?(wall_id)
        raise InvalidParams, "#{path}.wall names #{wall_id.inspect}, which is not in the plan; walls are #{walls_by_id.keys.join(', ')}"
      end

      kind = choice(raw['opening_kind'], "#{path}.opening_kind", OPENING_KINDS)
      door = kind == 'door'
      sill = if raw['sill'].nil?
               door ? 0.0 : config[:window_default_sill_mm]
             else
               non_negative(raw['sill'], "#{path}.sill")
             end
      height = if raw['height'].nil?
                 door ? config[:door_default_height_mm] : config[:window_default_height_mm]
               else
                 positive(raw['height'], "#{path}.height")
               end
      {
        'id' => id,
        'wall' => wall_id,
        'opening_kind' => kind,
        'offset' => non_negative(raw['offset'], "#{path}.offset"),
        'width' => positive(raw['width'], "#{path}.width"),
        'sill' => sill,
        'height' => height,
        'swing' => choice(raw.fetch('swing', 'in') || 'in', "#{path}.swing", SWINGS),
        'hand' => choice(raw.fetch('hand', 'left') || 'left', "#{path}.hand", HANDS),
        'tag' => raw['tag'].nil? ? id : text(raw['tag'], "#{path}.tag")
      }.tap { |o| opening_extras(o, raw, path) }
    end

    # 0.5 fields, stored only when used so a 0.4 record stays byte-identical:
    # an arched head with its rise, and the widest flat panel on an arc wall.
    def opening_extras(out, raw, path)
      head = choice(raw['head'] || 'flat', "#{path}.head", HEADS)
      if head == 'arch'
        half = out['width'] / 2.0
        rise = raw['arch_rise'].nil? ? half : positive(raw['arch_rise'], "#{path}.arch_rise")
        if rise > half + 1e-9
          raise InvalidParams, "#{path}.arch_rise must be at most half the width (#{fmt(half)}), got #{fmt(rise)}"
        end
        if rise > out['height'] + 1e-9
          raise InvalidParams, "#{path}.arch_rise #{fmt(rise)} is taller than the opening (#{fmt(out['height'])})"
        end

        out['head'] = 'arch'
        out['arch_rise'] = rise
      end
      out['panel_width'] = positive(raw['panel_width'], "#{path}.panel_width") unless raw['panel_width'].nil?
      out
    end

    # --- 0.5 records ------------------------------------------------------------

    def slab(raw, path, config = CONFIG)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      max = config[:slab_top_offset_max_mm]
      top = raw['top_offset'].nil? ? 0.0 : number(raw['top_offset'], "#{path}.top_offset")
      raise InvalidParams, "#{path}.top_offset must be within +-#{fmt(max)} mm, got #{fmt(top)}" if top.abs > max

      band = raw['edge_band']
      unless band.nil?
        object!(band, "#{path}.edge_band")
        band = { 'height' => positive(band['height'], "#{path}.edge_band.height"),
                 'depth' => positive(band['depth'], "#{path}.edge_band.depth") }
      end
      {
        'id' => id,
        'outline' => ring(raw['outline'], "#{path}.outline", "slab #{id}"),
        'holes' => list(raw, 'holes').each_with_index.map { |h, i| ring(h, "#{path}.holes[#{i}]", "slab #{id} hole #{i}") },
        'thickness' => raw['thickness'].nil? ? config[:slab_thickness_mm] : positive(raw['thickness'], "#{path}.thickness"),
        'top_offset' => top,
        'material' => raw['material'].nil? ? 'MAT_piso_porcelanato' : text(raw['material'], "#{path}.material"),
        'edge_band' => band
      }
    end

    def railing(raw, path, config = CONFIG)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      closed = bool(raw, 'closed', false, "#{path}.closed")
      {
        'id' => id,
        'path' => vertices(raw['path'], "#{path}.path", "railing #{id}", closed, closed ? 3 : 2),
        'closed' => closed,
        'style' => choice(raw['style'] || 'glass', "#{path}.style", RAILING_STYLES),
        'height' => opt_positive(raw, 'height', config[:railing_height_mm], path),
        'inset' => raw['inset'].nil? ? config[:railing_inset_mm] : non_negative(raw['inset'], "#{path}.inset"),
        'on' => raw['on'].nil? ? nil : text(raw['on'], "#{path}.on"),
        'rail' => bool(raw, 'rail', false, "#{path}.rail")
      }
    end

    def planter(raw, path, config = CONFIG)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      height = opt_positive(raw, 'height', config[:planter_height_mm], path)
      soil = raw['soil_level'].nil? ? [config[:planter_soil_mm], height].min : non_negative(raw['soil_level'], "#{path}.soil_level")
      raise InvalidParams, "#{path}.soil_level #{fmt(soil)} is above the curb height #{fmt(height)}" if soil > height + 1e-9

      plants = raw['plants']
      unless plants.nil?
        object!(plants, "#{path}.plants")
        keys = plants['keys']
        unless keys.is_a?(Array) && !keys.empty?
          raise InvalidParams, "#{path}.plants.keys must be a list of at least one library key, got #{keys.inspect}"
        end

        seed = plants['seed'].nil? ? 0 : plants['seed']
        raise InvalidParams, "#{path}.plants.seed must be a whole number, got #{seed.inspect}" unless seed.is_a?(Integer)

        plants = { 'keys' => keys.each_with_index.map { |k, i| text(k, "#{path}.plants.keys[#{i}]") },
                   'spacing' => opt_positive(plants, 'spacing', config[:planter_spacing_mm], "#{path}.plants"),
                   'seed' => seed }
      end
      {
        'id' => id,
        'outline' => ring(raw['outline'], "#{path}.outline", "planter #{id}"),
        'curb_thickness' => opt_positive(raw, 'curb_thickness', config[:planter_curb_mm], path),
        'height' => height,
        'soil_level' => soil,
        'on' => raw['on'].nil? ? nil : text(raw['on'], "#{path}.on"),
        'plants' => plants
      }
    end

    def column(raw, path, _config = CONFIG)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      out = { 'id' => id }
      if raw['outline'].nil?
        out['at'] = point(raw['at'], "#{path}.at")
        out['diameter'] = positive(raw['diameter'], "#{path}.diameter")
      else
        unless raw['at'].nil? && raw['diameter'].nil?
          raise InvalidParams, "#{path} takes either at and diameter (round) or outline, not both"
        end

        out['outline'] = ring(raw['outline'], "#{path}.outline", "column #{id}")
      end
      out['height'] = raw['height'].nil? ? nil : positive(raw['height'], "#{path}.height")
      out['material'] = raw['material'].nil? ? 'MAT_hormigon' : text(raw['material'], "#{path}.material")
      out
    end

    def curtain_wall(raw, path, config = CONFIG)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      closed = bool(raw, 'closed', false, "#{path}.closed")
      sill = raw['sill'].nil? ? 0.0 : non_negative(raw['sill'], "#{path}.sill")
      head = raw['head'].nil? ? nil : positive(raw['head'], "#{path}.head")
      raise InvalidParams, "#{path}.head #{fmt(head)} must be above the sill #{fmt(sill)}" if head && head <= sill

      transoms = list(raw, 'transoms').each_with_index.map { |z, i| positive(z, "#{path}.transoms[#{i}]") }
      profile = raw['profile'].nil? ? config[:curtain_profile_mm].dup : raw['profile']
      unless profile.is_a?(Array) && profile.size == 2
        raise InvalidParams, "#{path}.profile must be [face width, depth] in mm, got #{profile.inspect}"
      end

      {
        'id' => id,
        'path' => vertices(raw['path'], "#{path}.path", "curtain wall #{id}", closed, closed ? 3 : 2),
        'closed' => closed,
        'sill' => sill,
        'head' => head,
        'mullion_spacing' => opt_positive(raw, 'mullion_spacing', config[:curtain_mullion_spacing_mm], path),
        'transoms' => transoms.sort,
        'profile' => [positive(profile[0], "#{path}.profile[0]"), positive(profile[1], "#{path}.profile[1]")],
        'glass' => opt_positive(raw, 'glass', config[:curtain_glass_mm], path)
      }
    end

    def pool(raw, path, config = CONFIG)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      raised = raw.fetch('raised', 'auto')
      raised = 'auto' if raised.nil?
      unless ['auto', true, false].include?(raised)
        raise InvalidParams, "#{path}.raised must be auto, true or false, got #{raised.inspect}"
      end

      {
        'id' => id,
        'outline' => ring(raw['outline'], "#{path}.outline", "pool #{id}"),
        'depth' => opt_positive(raw, 'depth', config[:pool_depth_mm], path),
        'coping' => raw['coping'].nil? ? config[:pool_coping_mm] : non_negative(raw['coping'], "#{path}.coping"),
        'raised' => raised,
        'on' => raw['on'].nil? ? nil : text(raw['on'], "#{path}.on")
      }
    end

    def ramp(raw, path, config = CONFIG)
      object!(raw, path)
      version!(raw, path)
      id = text(raw['id'], "#{path}.id")
      axis = vertices(raw['axis'], "#{path}.axis", "ramp #{id}", false, 2)
      z0 = raw['z_start'].nil? ? 0.0 : number(raw['z_start'], "#{path}.z_start")
      z1 = number(raw['z_end'], "#{path}.z_end")
      run = (0...(axis.size - 1)).sum { |i| Geometry::Arc.length(axis[i][0, 2], axis[i + 1][0, 2], Geometry::Arc.bulge_of(axis[i])) }
      slope = (z1 - z0).abs / run
      max = config[:ramp_max_slope]
      if slope > max + 1e-9
        raise InvalidParams, "ramp #{id} climbs #{fmt((z1 - z0).abs.round(1))} mm over #{fmt(run.round(1))} mm, a slope of " \
                             "#{fmt((slope * 100).round(1))} percent; the most is #{fmt((max * 100).round(1))} percent"
      end

      {
        'id' => id,
        'axis' => axis,
        'width' => positive(raw['width'], "#{path}.width"),
        'z_start' => z0,
        'z_end' => z1,
        'side_walls' => bool(raw, 'side_walls', true, "#{path}.side_walls"),
        'wall_height' => opt_positive(raw, 'wall_height', config[:ramp_wall_height_mm], path)
      }
    end

    def opt_positive(raw, key, default, path)
      raw[key].nil? ? default.to_f : positive(raw[key], "#{path}.#{key}")
    end

    def bool(raw, key, default, path)
      value = raw.fetch(key, default)
      value = default if value.nil?
      raise InvalidParams, "#{path} must be true or false, got #{value.inspect}" unless [true, false].include?(value)

      value
    end

    def room(raw, path)
      object!(raw, path)
      version!(raw, path)
      number = raw['number'].nil? ? nil : text(raw['number'], "#{path}.number")
      {
        'id' => text(raw['id'], "#{path}.id"),
        'name' => text(raw['name'], "#{path}.name"),
        'number' => number,
        'at' => point(raw['at'], "#{path}.at"),
        'area' => raw['area'].nil? ? 0.0 : non_negative(raw['area'], "#{path}.area", MAX_LENGTH_MM**2)
      }
    end

    # A stair record (AutoCAD MCP Pro): start is the midpoint of the bottom
    # riser, direction_deg turns the stair frame, risers counts every rise from
    # this floor to the next.
    def stair(raw, path)
      object!(raw, path)
      version!(raw, path)
      risers = raw['risers']
      unless risers.is_a?(Integer) && risers >= 2
        raise InvalidParams, "#{path}.risers must be a whole number of at least 2, got #{risers.inspect}"
      end

      {
        'id' => text(raw['id'], "#{path}.id"),
        'start' => point(raw['start'], "#{path}.start"),
        'direction_deg' => number(raw['direction_deg'], "#{path}.direction_deg"),
        'width' => positive(raw['width'], "#{path}.width"),
        'risers' => risers,
        'riser_height' => positive(raw['riser_height'], "#{path}.riser_height"),
        'going' => positive(raw['going'], "#{path}.going"),
        # Records carry the stair's kind as stair_kind ('kind' is the record kind, 'stair').
        'kind' => choice(raw['stair_kind'] || (STAIR_KINDS.include?(raw['kind']) ? raw['kind'] : 'straight'),
                         "#{path}.stair_kind", STAIR_KINDS),
        'turn' => choice(raw.fetch('turn', 'left'), "#{path}.turn", TURNS)
      }
    end

    # A catalogue block placed in the plan (AutoCAD MCP Pro arch_catalogue_insert):
    # item is the catalogue name, at the back-left corner, rotation degrees CCW.
    # An item Plomada has no massing for is kept here and skipped by the build.
    def furniture(raw, path)
      object!(raw, path)
      version!(raw, path)
      {
        'id' => text(raw['id'], "#{path}.id"),
        'item' => text(raw['item'], "#{path}.item"),
        'at' => point(raw['at'], "#{path}.at"),
        'rotation' => raw['rotation'].nil? ? 0.0 : number(raw['rotation'], "#{path}.rotation")
      }
    end

    SITE_POLYGONS = %w[paving decks pools].freeze

    # The site around the house (from the SITIO_ blocks and SITIO-* layers):
    # objects are placed library components; paving, decks and pools are
    # closed polygons; fences are polylines.
    def site(raw)
      raw ||= {}
      raise InvalidParams, "site must be an object, got #{type_name(raw)}" unless raw.is_a?(Hash)

      out = {}
      out['objects'] = list(raw, 'objects').each_with_index.map do |o, i|
        path = "site.objects[#{i}]"
        object!(o, path)
        { 'id' => text(o['id'], "#{path}.id"), 'item' => text(o['item'], "#{path}.item"),
          'at' => point(o['at'], "#{path}.at"),
          'rotation' => o['rotation'].nil? ? 0.0 : number(o['rotation'], "#{path}.rotation") }
      end
      SITE_POLYGONS.each do |key|
        out[key] = list(raw, key).each_with_index.map do |o, i|
          path = "site.#{key}[#{i}]"
          object!(o, path)
          id = text(o['id'], "#{path}.id")
          rec = { 'id' => id, 'points' => vertices(o['points'], "#{path}.points", "#{path} #{id}", true, 3) }
          rec['depth'] = o['depth'].nil? ? 1500.0 : positive(o['depth'], "#{path}.depth", 5000.0) if key == 'pools'
          rec
        end
      end
      out['fences'] = list(raw, 'fences').each_with_index.map do |o, i|
        path = "site.fences[#{i}]"
        object!(o, path)
        id = text(o['id'], "#{path}.id")
        { 'id' => id, 'points' => vertices(o['points'], "#{path}.points", "#{path} #{id}", o['closed'] == true, 2),
          'closed' => o['closed'] == true,
          'height' => o['height'].nil? ? 1800.0 : positive(o['height'], "#{path}.height", 5000.0) }
      end
      %w[objects paving decks pools fences].each { |k| unique!(out[k], "site.#{k}") }
      out
    end

    def points(value, path, min)
      unless value.is_a?(Array) && value.size >= min
        raise InvalidParams, "#{path} must be a list of at least #{min} [x, y] points, got #{value.inspect[0, 60]}"
      end

      value.each_with_index.map { |p, i| point(p, "#{path}[#{i}]") }
    end

    # A polyline of [x, y] or [x, y, bulge] vertices. Zero bulges are dropped,
    # a bulge above a semicircle is refused naming the segment, and the bulge
    # of an open polyline's last vertex (no segment follows it) is ignored.
    def vertices(value, path, owner, closed, min = 2)
      unless value.is_a?(Array) && value.size >= min
        raise InvalidParams, "#{path} must be a list of at least #{min} [x, y] or [x, y, bulge] points, " \
                             "got #{value.inspect[0, 60]}"
      end

      out = value.each_with_index.map { |v, i| vertex(v, "#{path}[#{i}]") }
      out[-1] = out[-1][0, 2] unless closed || out.empty?
      out.each_with_index { |v, i| Geometry::Arc.check_bulge!(v[2], i, owner) if v.size > 2 }
      out
    end

    def vertex(value, path)
      unless value.is_a?(Array) && [2, 3].include?(value.size)
        raise InvalidParams, "#{path} must be [x, y] or [x, y, bulge] in mm, got #{value.inspect}"
      end

      xy = point(value[0, 2], path)
      return xy if value.size == 2

      b = number(value[2], "#{path}[2]")
      b.abs <= 1e-12 ? xy : xy + [b]
    end

    # A closed ring of at least 3 vertices (or 2 joined by arcs) with a non-zero area.
    def ring(value, path, owner)
      pts = vertices(value, path, owner, true, 2)
      if pts.size < 3 && pts.none? { |v| v.size > 2 }
        raise InvalidParams, "#{path} must be a list of at least 3 [x, y] or [x, y, bulge] points, got #{pts.size}"
      end

      area = Geometry.signed_area(Geometry::Arc.polyline(pts, closed: true))
      raise InvalidParams, "#{path} encloses no area" if area.abs < 1.0

      pts
    end

    # --- field readers --------------------------------------------------------

    def list(raw, key)
      value = raw.fetch(key, [])
      value = [] if value.nil?
      raise InvalidParams, "#{key} must be a list, got #{type_name(value)}" unless value.is_a?(Array)

      value
    end

    def object!(raw, path)
      raise InvalidParams, "#{path} must be an object, got #{type_name(raw)}" unless raw.is_a?(Hash)
    end

    def version!(raw, path)
      return unless raw.key?('v')
      return if raw['v'] == RECORD_VERSION

      raise InvalidParams, "#{path}.v is #{raw['v'].inspect}; Plomada reads record version #{RECORD_VERSION} only"
    end

    def unique!(items, path)
      seen = {}
      items.each_with_index do |item, i|
        if seen.key?(item['id'])
          raise InvalidParams, "#{path}[#{i}].id repeats #{item['id'].inspect} from #{path}[#{seen[item['id']]}]"
        end

        seen[item['id']] = i
      end
    end

    def number(value, path)
      if value.nil?
        raise InvalidParams, "#{path} is required"
      elsif !value.is_a?(Numeric) || value.is_a?(Complex)
        raise InvalidParams, "#{path} must be a number, got #{value.inspect}"
      end

      f = value.to_f
      raise InvalidParams, "#{path} must be finite, got #{value.inspect}" unless f.finite?

      f
    end

    def positive(value, path, max = MAX_LENGTH_MM)
      f = number(value, path)
      raise InvalidParams, "#{path} must be greater than 0, got #{fmt(f)}" unless f.positive?
      raise InvalidParams, "#{path} must be at most #{fmt(max)}, got #{fmt(f)}" if f > max

      f
    end

    def non_negative(value, path, max = MAX_LENGTH_MM)
      f = number(value, path)
      raise InvalidParams, "#{path} must be greater than or equal to 0, got #{fmt(f)}" if f.negative?
      raise InvalidParams, "#{path} must be at most #{fmt(max)}, got #{fmt(f)}" if f > max

      f
    end

    def point(value, path)
      unless value.is_a?(Array) && value.size == 2
        raise InvalidParams, "#{path} must be [x, y] in mm, got #{value.inspect}"
      end

      x = number(value[0], "#{path}[0]")
      y = number(value[1], "#{path}[1]")
      if x.abs > MAX_LENGTH_MM || y.abs > MAX_LENGTH_MM
        raise InvalidParams, "#{path} lies more than 1 km from the origin; lengths are millimetres"
      end

      [x, y]
    end

    def text(value, path)
      value = value.to_s if value.is_a?(Integer)
      raise InvalidParams, "#{path} must be a string, got #{value.inspect}" unless value.is_a?(String)

      stripped = value.strip
      raise InvalidParams, "#{path} must not be empty" if stripped.empty?

      stripped
    end

    def choice(value, path, allowed)
      v = value.is_a?(String) ? value.strip.downcase : value
      return v if allowed.include?(v)

      raise InvalidParams, "#{path} must be one of #{allowed.join(', ')}, got #{value.inspect}"
    end

    def fmt(f)
      f == f.round ? f.round.to_s : f.round(6).to_s
    end

    def dist(a, b)
      Math.hypot(b[0] - a[0], b[1] - a[1])
    end

    def type_name(value)
      case value
      when Hash then 'an object'
      when Array then 'a list'
      when String then 'a string'
      when nil then 'null'
      else value.class.name.downcase
      end
    end
  end
end
