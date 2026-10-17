# frozen_string_literal: true

require 'json'
require_relative 'config'
require_relative 'security'

module Plomada
  # Extensions > Plomada > Settings: port, busy tick, token file, allow_ruby,
  # the last audit lines and the max tick. Closing goes through a zero timer,
  # never from inside the dialog's own callback.
  module SettingsDialog
    HTML = <<~HTML
      <!doctype html><html><head><meta charset="utf-8"><title>Plomada</title>
      <style>
        body{font:13px "Segoe UI",sans-serif;margin:16px;color:#222;background:#fafafa}
        h1{font-size:16px;margin:0 0 12px} label{display:block;margin:8px 0 2px;font-weight:600}
        input[type=number]{width:120px} code,pre{background:#eee;padding:2px 4px;border-radius:3px}
        pre{max-height:220px;overflow:auto;font-size:11px;padding:6px;white-space:pre-wrap}
        .row{display:flex;gap:16px} .muted{color:#666} .warn{color:#a40} button{margin:12px 8px 0 0;padding:4px 14px}
      </style></head><body>
      <h1>Plomada <span id="ver" class="muted"></span></h1>
      <div id="state" class="muted"></div>
      <div class="row">
        <div><label>Port (1024-65535)</label><input id="port" type="number" min="1024" max="65535">
          <span id="portnote" class="muted"></span></div>
        <div><label>Busy tick, ms (10-200)</label><input id="tick" type="number" min="10" max="200"></div>
      </div>
      <label>Token file</label><code id="token"></code>
      <label><input id="ruby" type="checkbox"> Allow execute_ruby</label>
      <div class="warn">Runs any Ruby a connected client sends, inside SketchUp. Leave it off unless you need it.</div>
      <label>Max tick</label><span id="maxtick"></span> ms
      <label>Last audit lines</label><pre id="audit"></pre>
      <div id="msg" class="warn"></div>
      <button onclick="save()">Save</button><button onclick="sketchup.ready()">Refresh</button><button onclick="sketchup.close()">Close</button>
      <script>
        function render(d){
          document.getElementById('ver').textContent=d.version;
          document.getElementById('state').textContent=d.state;
          document.getElementById('port').value=d.port;
          document.getElementById('port').disabled=d.port_from_env;
          document.getElementById('portnote').textContent=d.port_from_env?'from PLOMADA_PORT':'';
          document.getElementById('tick').value=d.tick_ms;
          document.getElementById('token').textContent=d.token_path;
          document.getElementById('ruby').checked=d.allow_ruby;
          document.getElementById('maxtick').textContent=d.max_tick_ms;
          document.getElementById('audit').textContent=d.audit.join('\\n')||'(empty)';
          document.getElementById('msg').textContent=d.message||'';
        }
        function save(){
          sketchup.save(JSON.stringify({port:parseInt(document.getElementById('port').value,10),
            tick_ms:parseInt(document.getElementById('tick').value,10),
            allow_ruby:document.getElementById('ruby').checked}));
        }
        window.onload=function(){sketchup.ready();};
      </script></body></html>
    HTML

    module_function

    def show
      if @dialog&.visible?
        @dialog.bring_to_front
        return
      end
      @dialog = UI::HtmlDialog.new(dialog_title: 'Plomada settings', preferences_key: 'Plomada.settings',
                                   scrollable: true, resizable: true, width: 560, height: 640,
                                   style: UI::HtmlDialog::STYLE_DIALOG)
      @dialog.set_html(HTML)
      @dialog.add_action_callback('ready') { |_ctx| push }
      @dialog.add_action_callback('save') { |_ctx, json| save(json) }
      @dialog.add_action_callback('close') { |_ctx| UI.start_timer(0, false) { @dialog&.close } }
      @dialog.show
    end

    def snapshot(message = nil)
      settings = Settings.new(Sketchup)
      server = App.server
      state = if server&.running?
                "listening on #{CONFIG[:host]}:#{server.port}, #{server.clients.size} client(s)"
              else
                "not running#{App.last_error ? ": #{App.last_error}" : ''}"
              end
      {
        'version' => VERSION, 'state' => state, 'port' => settings.port,
        'port_from_env' => settings.port_from_env?, 'tick_ms' => settings.tick_ms,
        'allow_ruby' => settings.allow_ruby?, 'token_path' => Paths.token_path,
        'max_tick_ms' => server ? server.max_tick_ms.round(1) : '-',
        'audit' => App.audit.tail(CONFIG[:audit_dialog_lines]), 'message' => message
      }
    end

    def push(message = nil)
      @dialog&.execute_script("render(#{JSON.generate(snapshot(message))})")
    end

    def save(json)
      data = JSON.parse(json.to_s)
      before = Settings.new(Sketchup).snapshot
      Settings.new(Sketchup).update(port: data['port'], tick_ms: data['tick_ms'], allow_ruby: data['allow_ruby'] == true)
      after = Settings.new(Sketchup).snapshot
      restart = before['port'] != after['port'] || before['tick_ms'] != after['tick_ms']
      App.restart if restart
      push(restart ? 'Saved; the server restarted on the new settings.' : 'Saved.')
    rescue Plomada::Error, JSON::ParserError => e
      push("Not saved: #{e.message}")
    end
  end
end
