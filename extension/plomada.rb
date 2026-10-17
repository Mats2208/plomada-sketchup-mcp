# frozen_string_literal: true

# Plomada: a Model Context Protocol bridge for SketchUp that models
# architecture from AutoCAD MCP Pro plans. This loader only registers the
# extension; everything else lives in the plomada/ folder.
#
# PLOMADA_DEV_DIR names another folder holding main.rb (a git worktree's
# extension/plomada); when it is set and valid, the extension loads from there
# instead of Plugins\plomada. Unset, nothing changes.
require 'sketchup.rb'
require 'extensions.rb'

module Plomada
  dev_dir = ENV['PLOMADA_DEV_DIR'].to_s.tr('\\', '/')
  PLUGIN_DIR = if !dev_dir.empty? && File.file?(File.join(dev_dir, 'main.rb'))
                 File.expand_path(dev_dir)
               else
                 File.join(__dir__, 'plomada').tr('\\', '/')
               end
  require File.join(PLUGIN_DIR, 'version')

  unless file_loaded?(__FILE__)
    extension = SketchupExtension.new(EXTENSION_NAME, File.join(PLUGIN_DIR, 'main'))
    extension.description = 'Local MCP server for Claude: builds an exact 3D house from an AutoCAD MCP Pro plan. ' \
                            'Loopback only, token authenticated.'
    extension.version = VERSION
    extension.creator = 'Mats2208'
    extension.copyright = '2026 Mats2208, MIT License'
    Sketchup.register_extension(extension, true)
    file_loaded(__FILE__)
  end
end
