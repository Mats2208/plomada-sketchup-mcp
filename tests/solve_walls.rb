# frozen_string_literal: true

# Solves the walls of a plan JSON file with the extension's pure-Ruby solver (no SketchUp) and prints
# {"ok": true, "manifold": ..., "openings": n} or {"ok": false, "error": "..."}; tests/test_infer.py
# uses it to prove an inferred plan is one the solver accepts.
require 'json'
$LOAD_PATH.unshift File.expand_path('../extension', __dir__)
require 'plomada/errors'
require 'plomada/config'
require 'plomada/plan'
require 'plomada/geometry'

begin
  plan = Plomada::Plan.normalize(JSON.parse(File.read(ARGV[0], encoding: 'UTF-8')))
  layout = Plomada::Geometry.solve_walls(plan['walls'], plan['openings'], storey_height: 2800.0)
  rep = Plomada::Geometry.manifold_report(layout[:faces].map { |f| { outer: f[:outer_ids], holes: f[:hole_ids] } })
  puts JSON.generate(ok: true, manifold: rep[:manifold], openings: layout[:openings].size)
rescue Plomada::InvalidParams => e
  puts JSON.generate(ok: false, error: e.message)
end
