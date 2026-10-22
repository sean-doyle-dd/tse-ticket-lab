(function () {
  "use strict";
  const form = document.getElementById("generate-form");
  if (!form) return;
  const errEl = document.getElementById("generate-error");
  // Only offer difficulties that a real ticket backs; stretching one would mean inventing detail.
  const counts = JSON.parse(form.dataset.difficultyCounts || "{}");
  function syncDifficulties() {
    const spec = new FormData(form).get("spec");
    const radios = [...form.querySelectorAll("input[name=difficulty]")];
    radios.forEach((r) => {
      const ok = !!(spec && (counts[spec] || {})[r.value]);
      r.disabled = !ok;
      r.closest("label").classList.toggle("disabled", !ok);
      if (!ok) r.checked = false;
    });
    if (!radios.some((r) => r.checked)) {
      const first = radios.find((r) => !r.disabled);
      if (first) first.checked = true;
    }
  }
  form.querySelectorAll("input[name=spec]").forEach((r) => r.addEventListener("change", syncDifficulties));
  syncDifficulties();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errEl.textContent = "";
    const data = new FormData(form);
    const spec = data.get("spec"), difficulty = data.get("difficulty");
    if (!spec || !difficulty) { errEl.textContent = "Pick a spec and a difficulty."; return; }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      const res = await TG.api("/api/scenarios", { method: "POST", body: JSON.stringify({ spec, difficulty }) });
      window.location.href = res.url;
    } catch (err) {
      errEl.textContent = err.message;
      btn.disabled = false;
      btn.textContent = "Generate ticket";
    }
  });
})();
