// User Lookup type-ahead (operations/user_lookup/lookup.html), the same
// script as netskope-portal's user status page:
//
//   <div class="typeahead" data-suggest-url="/operations/user-lookup/suggest">
//     <input role="combobox" ...> <ul role="listbox" hidden></ul> <div data-typeahead-status></div>
//   </div>
//
// Suggestions come from a POST (with the page's CSRF token) so typed text never lands in a URL.
// Rendered with textContent only: suggestion text comes from the tenant and is never parsed as HTML.
(function () {
  var MIN_CHARS = 3, DELAY_MS = 250;

  document.querySelectorAll(".typeahead[data-suggest-url]").forEach(function (box) {
    var input = box.querySelector("input");
    var list = box.querySelector("[role=listbox]");
    var status = box.querySelector("[data-typeahead-status]");
    var form = input.form;
    var url = box.getAttribute("data-suggest-url");
    var csrf = form && form.querySelector("input[name=csrf_token]");
    var cache = {}, timer = null, controller = null, seq = 0, items = [], active = -1;

    function close() {
      list.hidden = true;
      list.textContent = "";
      items = [];
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function highlight(el, value, typed) {
      var i = value.toLowerCase().indexOf(typed.toLowerCase());
      if (i < 0) { el.textContent = value; return; }
      var mark = document.createElement("mark");
      mark.textContent = value.slice(i, i + typed.length);
      el.append(value.slice(0, i), mark, value.slice(i + typed.length));
    }

    function note(text) {
      var li = document.createElement("li");
      li.className = "ta-note";
      li.setAttribute("aria-disabled", "true");
      li.textContent = text;
      list.appendChild(li);
    }

    function render(data, typed) {
      close();
      items = data.items || [];
      items.forEach(function (it, i) {
        var li = document.createElement("li");
        li.id = "q-opt-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        li.dataset.index = i;
        var kind = document.createElement("span");
        kind.className = "ta-kind";
        kind.textContent = it.kind === "host" ? "Host" : "User";
        var text = document.createElement("span");
        text.className = "ta-text";
        var value = document.createElement("span");
        value.className = "ta-value";
        highlight(value, it.value, typed);
        text.appendChild(value);
        if (it.detail) {
          var detail = document.createElement("span");
          detail.className = "ta-detail";
          detail.textContent = it.detail;
          text.appendChild(detail);
        }
        li.append(kind, text);
        list.appendChild(li);
      });
      if (data.error) note(data.error);
      else if (!items.length) note("No matches. Press Look up to search anyway.");
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      status.textContent = items.length ? items.length + " suggestions. Use the arrow keys to choose." : "";
    }

    function move(to) {
      if (!items.length) return;
      if (active >= 0) list.children[active].setAttribute("aria-selected", "false");
      active = (to + items.length) % items.length;
      var li = list.children[active];
      li.setAttribute("aria-selected", "true");
      li.scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", li.id);
    }

    function choose(i) {
      input.value = items[i].value;
      close();
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    }

    function fetchSuggestions(typed) {
      var key = typed.toLowerCase();
      if (cache[key]) { render(cache[key], typed); return; }
      if (controller) controller.abort();
      controller = window.AbortController ? new AbortController() : null;
      var mine = ++seq;
      var body = new URLSearchParams({ q: typed, csrf_token: csrf ? csrf.value : "" });
      fetch(url, {
        method: "POST", body: body, credentials: "same-origin",
        headers: { "Accept": "application/json" }, signal: controller ? controller.signal : undefined
      }).then(function (resp) {
        var json = (resp.headers.get("Content-Type") || "").indexOf("application/json") === 0;
        if (!json) throw new Error("not json");     // e.g. redirected to the connect page
        return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
      }).then(function (r) {
        if (mine !== seq || input.value.trim() !== typed) return;   // a newer request owns the list
        if (r.ok && !r.data.error) cache[key] = r.data;
        // A FastAPI HTTPException (e.g. an expired CSRF token, 403) carries its message in "detail".
        if (!r.ok && !r.data.error && r.data.detail) r.data = { items: [], error: String(r.data.detail) };
        render(r.data, typed);
      }).catch(function () {
        if (mine === seq) close();
      });
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      var typed = input.value.trim();
      if (typed.length < MIN_CHARS) { seq++; close(); return; }
      timer = setTimeout(function () { fetchSuggestions(typed); }, DELAY_MS);
    });

    input.addEventListener("keydown", function (e) {
      var open = !list.hidden;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!open) return;
        e.preventDefault();
        move(active + (e.key === "ArrowDown" ? 1 : -1));
      } else if (e.key === "Enter" && open && active >= 0) {
        e.preventDefault();
        choose(active);
      } else if (e.key === "Escape" && open) {
        e.preventDefault();
        close();
      } else if (e.key === "Tab") {
        close();
      }
    });

    // mousedown, not click: keeps focus in the input so blur does not close the list first.
    list.addEventListener("mousedown", function (e) {
      var li = e.target.closest("[role=option]");
      e.preventDefault();
      if (li) choose(Number(li.dataset.index));
    });

    input.addEventListener("blur", function () { setTimeout(close, 100); });
  });
})();
