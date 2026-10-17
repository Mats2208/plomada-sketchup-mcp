# frozen_string_literal: true

require 'securerandom'
require 'fileutils'
require 'time'
require_relative 'config'
require_relative 'errors'

module Plomada
  # Where Plomada keeps its per-user files: %LOCALAPPDATA%\Plomada on Windows.
  # PLOMADA_HOME overrides it (tests, CI on Linux).
  module Paths
    module_function

    def data_dir
      return ENV['PLOMADA_HOME'] if ENV['PLOMADA_HOME'] && !ENV['PLOMADA_HOME'].empty?

      base = ENV['LOCALAPPDATA']
      base = File.join(Dir.home, '.local', 'share') if base.nil? || base.empty?
      File.join(base.tr('\\', '/'), 'Plomada')
    end

    def token_path = File.join(data_dir, 'bridge.token')
    def audit_path = File.join(data_dir, 'audit.log')
    def endpoint_path = File.join(data_dir, 'endpoint.json')
  end

  module Auth
    TOKEN_FORMAT = /\A[0-9a-f]{64}\z/

    module_function

    # Constant-time comparison: unequal lengths are rejected first, then every
    # byte is XOR-accumulated so the time does not depend on where they differ.
    def secure_compare(given, expected)
      return false unless given.is_a?(String) && expected.is_a?(String)

      a = given.b
      b = expected.b
      return false unless a.bytesize == b.bytesize

      diff = 0
      a.bytesize.times { |i| diff |= a.getbyte(i) ^ b.getbyte(i) }
      diff.zero?
    end

    # Reads the token, creating it on first load: SecureRandom.hex(32) written
    # to bridge.token.tmp, then renamed over bridge.token so a reader never
    # sees a half-written file.
    def load_or_create_token(path = Paths.token_path, bytes = CONFIG[:token_bytes])
      existing = File.file?(path) ? File.read(path, mode: 'rb').strip : nil
      return existing if existing&.match?(TOKEN_FORMAT)

      write_atomically(path, SecureRandom.hex(bytes))
    end

    def regenerate_token(path = Paths.token_path, bytes = CONFIG[:token_bytes])
      write_atomically(path, SecureRandom.hex(bytes))
    end

    def write_atomically(path, text)
      FileUtils.mkdir_p(File.dirname(path))
      tmp = "#{path}.tmp"
      File.open(tmp, File::WRONLY | File::CREAT | File::TRUNC | File::BINARY, 0o600) { |f| f.write(text) }
      File.rename(tmp, path)
      text
    end
  end

  # One line per mutating call: timestamp, client id, method, duration and
  # outcome. Never params, never the token. Rotates to audit.log.1 at the cap.
  class Audit
    attr_reader :path

    def initialize(path = Paths.audit_path, rotate_bytes = CONFIG[:audit_rotate_bytes])
      @path = path
      @rotate_bytes = rotate_bytes
    end

    def record(client_id:, method:, duration_ms:, outcome:)
      line = format("%<ts>s client=%<c>s method=%<m>s duration_ms=%<d>d outcome=%<o>s\n",
                    ts: Time.now.utc.iso8601(3), c: client_id, m: method, d: duration_ms.round, o: outcome)
      FileUtils.mkdir_p(File.dirname(@path))
      rotate if (File.size?(@path) || 0) + line.bytesize > @rotate_bytes
      File.open(@path, 'ab') { |f| f.write(line) }
      line
    rescue SystemCallError, IOError
      nil
    end

    def tail(count)
      return [] unless File.file?(@path)

      File.readlines(@path, chomp: true).last(count)
    rescue SystemCallError, IOError
      []
    end

    private

    def rotate
      File.rename(@path, "#{@path}.1")
    rescue SystemCallError
      nil
    end
  end

  # Settings live in SketchUp's defaults under "Plomada" and are range-checked
  # on every read; an out-of-range value falls back to the CONFIG default.
  # PLOMADA_PORT overrides the stored port for this process only: the stored
  # one is never rewritten while the override is on, so a second SketchUp
  # started with it leaves the first one's port alone.
  class Settings
    SECTION = 'Plomada'
    PORT_ENV = 'PLOMADA_PORT'

    attr_reader :warnings

    def initialize(store, config = CONFIG, env = ENV)
      @store = store
      @cfg = config
      @env = env
      @warnings = []
    end

    def port = env_port || ranged('port', @cfg[:port], @cfg[:port_range])
    def port_from_env? = !env_port.nil?
    def tick_ms = ranged('tick_ms', @cfg[:tick_busy_ms], @cfg[:tick_range_ms])

    def allow_ruby?
      @store.read_default(SECTION, 'allow_ruby', false) == true
    end

    def update(port: nil, tick_ms: nil, allow_ruby: nil)
      write_ranged('port', port, @cfg[:port_range]) unless port.nil? || port_from_env?
      write_ranged('tick_ms', tick_ms, @cfg[:tick_range_ms]) unless tick_ms.nil?
      # allow_ruby is written last, so a refused port or tick leaves it untouched.
      @store.write_default(SECTION, 'allow_ruby', allow_ruby == true) unless allow_ruby.nil?
    end

    def snapshot
      { 'port' => port, 'port_from_env' => port_from_env?, 'tick_ms' => tick_ms, 'allow_ruby' => allow_ruby? }
    end

    private

    def env_port
      raw = @env[PORT_ENV].to_s.strip
      return nil if raw.empty?

      lo, hi = @cfg[:port_range]
      value = Integer(raw, 10, exception: false)
      return value if value&.between?(lo, hi)

      @warnings << "#{PORT_ENV}=#{raw.inspect} is outside #{lo}..#{hi}; using the stored port" unless @env_warned
      @env_warned = true
      nil
    end

    def ranged(key, default, (lo, hi))
      value = @store.read_default(SECTION, key, default)
      return value if value.is_a?(Integer) && value.between?(lo, hi)

      @warnings << "setting #{key}=#{value.inspect} is outside #{lo}..#{hi}; using #{default}"
      default
    end

    def write_ranged(key, value, (lo, hi))
      unless value.is_a?(Integer) && value.between?(lo, hi)
        raise InvalidParams, "#{key} must be an integer in #{lo}..#{hi}, got #{value.inspect}"
      end

      @store.write_default(SECTION, key, value)
    end
  end
end
