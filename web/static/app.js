/* FaceChain UI - drives POST /api/run then reads the SSE stream. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var MAX_LOG = 400;      // DOM caps
  var MAX_ROWS = 60;

  var state = {
    file: null,        // File object from picker/drop
    sample: null,      // bundled sample name
    running: false,
    es: null,
    threshold: 0.363,
    liveRows: {},      // index -> row element
    rowCount: 0
  };

  /* ------------------------------------------------ helpers */
  function txt(id, v) { var e = $(id); if (e) e.textContent = (v === null || v === undefined || v === "") ? "-" : String(v); }
  function stage(id, status) {
    var e = $(id); if (!e) return;
    e.className = "st " + (status || "");
    e.textContent = status || "idle";
  }
  function showErr(id, msg) {
    var e = $(id); if (!e) return;
    e.hidden = false; e.textContent = msg;
  }
  function clearErrs() {
    ["err-face", "err-search", "err-chain"].forEach(function (i) {
      var e = $(i); if (e) { e.hidden = true; e.textContent = ""; }
    });
  }
  function log(msg) {
    var box = $("log");
    var d = document.createElement("div");
    if (/MATCH/.test(msg) || /^done in/.test(msg)) d.className = "hit";
    d.textContent = msg;
    box.appendChild(d);
    while (box.childElementCount > MAX_LOG) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }
  function runState(msg, cls) {
    var e = $("runstate");
    e.textContent = msg;
    e.className = "runstate " + (cls || "");
  }

  /* ------------------------------------------------ health */
  fetch("/health").then(function (r) { return r.json(); }).then(function (h) {
    var m = $("chip-models");
    m.textContent = "models · " + (h.models_present ? "ready" : "missing");
    m.className = "chip " + (h.models_present ? "ok" : "bad");
    var c = $("chip-chain");
    c.textContent = "chain · " + (h.chain_backend || "?");
    c.className = "chip " + (h.chain_ok ? "ok" : "bad");
  }).catch(function () { });

  /* ------------------------------------------------ input wiring */
  var drop = $("drop"), fileIn = $("file"), thumb = $("drop-thumb");

  function setPreview(src) {
    thumb.src = src;
    drop.classList.add("has");
  }
  function pickFile(f) {
    if (!f) return;
    state.file = f; state.sample = null;
    Array.prototype.forEach.call(document.querySelectorAll(".sample"),
      function (b) { b.classList.remove("on"); });
    setPreview(URL.createObjectURL(f));
  }

  drop.addEventListener("click", function () { fileIn.click(); });
  fileIn.addEventListener("change", function () { pickFile(fileIn.files[0]); });
  ["dragenter", "dragover"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); });
  });
  drop.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      pickFile(e.dataTransfer.files[0]);
    }
  });

  Array.prototype.forEach.call(document.querySelectorAll(".sample"), function (btn) {
    btn.addEventListener("click", function () {
      Array.prototype.forEach.call(document.querySelectorAll(".sample"),
        function (b) { b.classList.remove("on"); });
      btn.classList.add("on");
      state.sample = btn.dataset.name;
      state.file = null;
      fileIn.value = "";
      $("hint").value = btn.dataset.hint || "";
      setPreview("/sample/" + btn.dataset.name);
    });
  });

  /* ------------------------------------------------ stage 1 canvas */
  function drawFace(imgUrl, bbox) {
    var wrap = $("face-canvas-wrap"), cv = $("face-canvas");
    var img = new Image();
    img.onload = function () {
      // fit the natural image into the available box, then scale the bbox with it
      var box = wrap.getBoundingClientRect();
      var maxW = Math.max(80, box.width - 2), maxH = Math.max(80, box.height - 2);
      var k = Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight, 4);
      var w = Math.round(img.naturalWidth * k), h = Math.round(img.naturalHeight * k);
      cv.width = w; cv.height = h;
      cv.style.width = w + "px"; cv.style.height = h + "px";
      var ctx = cv.getContext("2d");
      ctx.clearRect(0, 0, w, h);
      ctx.drawImage(img, 0, 0, w, h);
      if (bbox && bbox.length === 4) {
        var x = bbox[0] * k, y = bbox[1] * k, bw = bbox[2] * k, bh = bbox[3] * k;
        ctx.lineWidth = 1; ctx.strokeStyle = "#00e5ff";
        ctx.strokeRect(x + 0.5, y + 0.5, bw, bh);
        // corner ticks
        ctx.lineWidth = 2;
        var t = Math.max(6, Math.min(bw, bh) * 0.18);
        [[x, y, 1, 1], [x + bw, y, -1, 1], [x, y + bh, 1, -1], [x + bw, y + bh, -1, -1]]
          .forEach(function (c) {
            ctx.beginPath();
            ctx.moveTo(c[0] + c[2] * t, c[1]); ctx.lineTo(c[0], c[1]);
            ctx.lineTo(c[0], c[1] + c[3] * t); ctx.stroke();
          });
        ctx.fillStyle = "#00e5ff";
        ctx.fillRect(x, Math.max(0, y - 13), 52, 13);
        ctx.fillStyle = "#041b21";
        ctx.font = "500 9px 'JetBrains Mono', ui-monospace, Consolas, monospace";
        ctx.fillText("FACE 01", x + 4, Math.max(9, y - 3.5));
      }
      wrap.classList.add("has");
    };
    img.onerror = function () { showErr("err-face", "could not load the image back for display"); };
    img.src = imgUrl + "?t=" + Date.now();
  }

  /* ------------------------------------------------ stage 2 rows */
  function bar(sim, hit) {
    var pct = Math.max(2, Math.min(100, sim * 100));
    return '<div class="bar"><i style="width:' + pct.toFixed(1) + '%"></i></div>' +
      '<span class="score">' + sim.toFixed(3) + '</span>';
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function liveRow(d) {
    var box = $("results");
    if (box.querySelector(".ph")) box.innerHTML = "";
    var row = state.liveRows[d.index];
    if (!row) {
      if (state.rowCount >= MAX_ROWS) return;
      row = document.createElement("div");
      box.appendChild(row);
      state.liveRows[d.index] = row;
      state.rowCount++;
    }
    row.className = "res" + (d.is_match ? " hit" : "");
    row.innerHTML =
      '<span class="badge">' + esc(d.platform) + '</span>' +
      bar(d.similarity, d.is_match) +
      '<div class="res-t"><a>candidate ' + d.index + '/' + d.total +
      (d.is_match ? ' &middot; MATCH' : '') + '</a><small>resolving post metadata…</small></div>';
    box.scrollTop = box.scrollHeight;
  }

  function renderResults(results) {
    var box = $("results");
    box.innerHTML = "";
    state.liveRows = {}; state.rowCount = 0;
    if (!results || !results.length) {
      box.innerHTML = '<div class="ph">no scored candidates</div>';
      return;
    }
    results.slice(0, MAX_ROWS).forEach(function (r) {
      var row = document.createElement("div");
      row.className = "res" + (r.is_match ? " hit" : "") + (r.is_best ? " best" : "");
      var title = r.title || r.post_url;
      row.innerHTML =
        '<span class="badge">' + esc(r.platform) + '</span>' +
        bar(r.similarity, r.is_match) +
        '<div class="res-t"><a href="' + esc(r.post_url) + '" target="_blank" rel="noopener">' +
        (r.is_best ? '<span class="best-tag">BEST</span>' : '') + esc(title) + '</a>' +
        '<small>' + esc(r.post_url) + '</small></div>';
      box.appendChild(row);
    });
  }

  /* ------------------------------------------------ event handling */
  function handle(ev) {
    var d = ev.data || {};
    switch (ev.type) {

      case "stage":
        if (d.stage === "face") stage("st-face", d.status);
        if (d.stage === "search") stage("st-search", d.status);
        if (d.stage === "chain") stage("st-chain", d.status);
        break;

      case "log":
        log(d.message);
        break;

      case "face":
        txt("f-conf", d.detection_confidence);
        txt("f-faces", d.faces_found);
        txt("f-bbox", "[" + (d.bbox || []).join(", ") + "]");
        txt("f-det", d.detector);
        txt("f-enc", d.encoder);
        txt("f-dim", d.embedding_dim + "-d L2-normalised");
        txt("f-emb", d.embedding_sha256);
        drawFace(d.image_url, d.bbox);
        break;

      case "candidate":
        liveRow(d);
        break;

      case "search_done":
        txt("c-seen", d.candidates_seen);
        txt("c-dl", d.candidates_downloaded);
        txt("c-faces", d.candidates_with_faces);
        txt("c-above", d.above_threshold);
        txt("c-prov", (d.providers || []).join(", ") || "none");
        renderResults(d.results);
        var n = $("notes");
        if (d.notes && d.notes.length) {
          n.hidden = false;
          n.innerHTML = d.notes.map(function (x) { return "<div>· " + esc(x) + "</div>"; }).join("");
        } else { n.hidden = true; }
        break;

      case "record":
        $("canonical").textContent = d.canonical || "-";
        txt("ch-rid", d.record_id);
        txt("ch-rhash", d.record_hash);
        break;

      case "receipt":
        txt("r-backend", d.backend_description || d.backend);
        txt("r-chain", d.chain_id);
        txt("r-contract", d.contract_address);
        txt("r-tx", d.tx_hash);
        txt("r-block", d.block_number);
        txt("r-gas", d.gas_used || "-");
        break;

      case "verify":
        var v = $("v-verify");
        v.className = "verdict " + (d.verified ? "good" : "bad");
        $("v-verify-b").textContent = d.verified ? "VERIFIED" : "MISMATCH";
        $("v-verify-d").textContent = "computed " + short(d.computed_hash) +
          "  vs on-chain " + short(d.onchain_hash) +
          (d.block_number ? "  · block " + d.block_number : "");
        break;

      case "tamper":
        var t = $("v-tamper");
        var detected = !!d.tamper_detected;
        t.className = "verdict " + (detected ? "bad" : "good");
        $("v-tamper-b").textContent = detected ? "TAMPER DETECTED" : "NOT DETECTED (bug)";
        $("v-tamper-d").textContent = "flipped " + (d.tampered_field || "post.post_url") +
          " → computed " + short(d.computed_hash) + " ≠ " + short(d.onchain_hash);
        break;

      case "error":
        var target = d.where === "search" ? "err-search" : lastErrTarget();
        showErr(target, d.message || "unknown error");
        runState("error", "bad");
        break;

      case "done":
        finish(d);
        break;
    }
  }

  function short(h) {
    if (!h) return "0x0";
    return h.length > 18 ? h.slice(0, 10) + "…" + h.slice(-6) : h;
  }
  function lastErrTarget() {
    if ($("st-chain").classList.contains("running")) return "err-chain";
    if ($("st-search").classList.contains("running")) return "err-search";
    return "err-face";
  }

  function finish(d) {
    state.running = false;
    $("run").disabled = false;
    if (state.es) { state.es.close(); state.es = null; }
    ["st-face", "st-search", "st-chain"].forEach(function (i) {
      var e = $(i);
      if (e.classList.contains("running")) stage(i, d.status === "ok" ? "ok" : "fail");
    });
    if (d.status === "ok") {
      runState("done in " + d.elapsed + "s" +
        (d.verified ? " · verified" : "") +
        (d.tamper_detected ? " · tamper detected" : ""), "");
    } else if (d.status === "no-match") {
      runState("no match above threshold (" + d.elapsed + "s)", "bad");
    } else {
      runState("failed after " + (d.elapsed || "?") + "s", "bad");
    }
  }

  /* ------------------------------------------------ run */
  function reset() {
    clearErrs();
    ["st-face", "st-search", "st-chain"].forEach(function (i) { stage(i, "idle"); });
    $("log").innerHTML = "";
    $("results").innerHTML = '<div class="ph">candidates will stream in here</div>';
    $("notes").hidden = true;
    $("face-canvas-wrap").classList.remove("has");
    ["f-conf", "f-faces", "f-bbox", "f-det", "f-enc", "f-dim", "f-emb",
      "ch-rid", "ch-rhash", "r-backend", "r-chain", "r-contract", "r-tx",
      "r-block", "r-gas"].forEach(function (i) { txt(i, "-"); });
    ["c-seen", "c-dl", "c-faces", "c-above"].forEach(function (i) { txt(i, 0); });
    txt("c-prov", "-");
    $("canonical").textContent = "-";
    $("v-verify").className = "verdict"; $("v-verify-b").textContent = "pending";
    $("v-verify-d").textContent = "";
    $("v-tamper").className = "verdict"; $("v-tamper-b").textContent = "pending";
    $("v-tamper-d").textContent = "";
    state.liveRows = {}; state.rowCount = 0;
  }

  $("run").addEventListener("click", function () {
    if (state.running) return;
    if (!state.file && !state.sample) {
      runState("pick an image or a sample first", "bad");
      return;
    }
    state.running = true;
    $("run").disabled = true;
    reset();
    runState("uploading…", "busy");

    var fd = new FormData();
    if (state.file) fd.append("image", state.file);
    else fd.append("sample", state.sample);
    fd.append("hint", $("hint").value.trim());
    fd.append("backend", $("backend").value);
    fd.append("threshold", $("threshold").value);
    fd.append("max_candidates", $("maxcand").value);
    state.threshold = parseFloat($("threshold").value) || 0.363;

    fetch("/api/run", { method: "POST", body: fd })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
          return j;
        });
      })
      .then(function (j) {
        runState("running · " + j.run_id, "busy");
        var es = new EventSource(j.stream);
        state.es = es;
        es.onmessage = function (m) {
          var ev;
          try { ev = JSON.parse(m.data); } catch (e) { return; }
          try { handle(ev); } catch (e) { console.error(e, ev); }
        };
        es.onerror = function () {
          if (state.running) {
            // stream dropped before a terminal event
            es.close(); state.es = null;
            showErr(lastErrTarget(), "event stream disconnected");
            finish({ status: "error", elapsed: "?" });
          }
        };
      })
      .catch(function (e) {
        state.running = false;
        $("run").disabled = false;
        runState(String(e.message || e), "bad");
        showErr("err-face", String(e.message || e));
      });
  });
})();
