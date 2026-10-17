# frozen_string_literal: true

module Plomada
  # Single source of the extension version; scripts/build_rbz.py and the
  # bridge's pyproject.toml are checked against it by the test suites.
  VERSION = '0.5.0'
  EXTENSION_NAME = 'Plomada'
end
