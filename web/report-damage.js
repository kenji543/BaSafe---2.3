(() => {
  "use strict";

  const BASEY_CENTER = [11.282, 125.069];
  const BASEY_FALLBACK_BOUNDS = [[11.2540, 124.9764], [11.5641, 125.3092]];
  const MAX_PHOTOS = 3;
  const MAX_EDGE = 1600;
  const STEPS = ["location", "details", "photos", "contact"];
  const byId = (id) => document.getElementById(id);
  const form = byId("report-form");
  const locationStatus = byId("location-status");
  const photoStatus = byId("photo-status");
  const formStatus = byId("form-status");
  const photos = [null, null, null];
  let pin = null;
  let marker = null;
  let lookup = 0;

  function say(element, message, kind = "") {
    element.textContent = message;
    element.dataset.kind = kind;
  }

  fetch("/api/v1/reports", { headers: { Accept: "application/json" } })
    .then((response) => {
      if (response.status === 503) {
        form.hidden = true;
        byId("report-unavailable").hidden = false;
      }
    })
    .catch(() => {});

  // ---------------- Step navigation ----------------
  function showStep(id) {
    document.querySelectorAll(".rd-step[data-step]").forEach((section) => {
      section.hidden = section.dataset.step !== id;
    });
    window.scrollTo({ top: 0, behavior: "auto" });
    const heading = document.querySelector(`.rd-step[data-step="${id}"] h1`);
    heading?.setAttribute("tabindex", "-1");
    heading?.focus({ preventScroll: true });
  }
  document.querySelectorAll("[data-next]").forEach((button) => {
    button.addEventListener("click", () => showStep(button.dataset.next));
  });
  document.querySelectorAll("[data-back]").forEach((button) => {
    button.addEventListener("click", () => {
      const current = document.querySelector(".rd-step[data-step]:not([hidden])")?.dataset.step;
      const index = STEPS.indexOf(current);
      if (index > 0) showStep(STEPS[index - 1]);
    });
  });

  // ---------------- Map / location ----------------
  const map = L.map("report-map", {
    maxBounds: L.latLngBounds(BASEY_FALLBACK_BOUNDS).pad(0.3),
    maxBoundsViscosity: 0.8
  }).setView(BASEY_CENTER, 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  const pinIcon = L.divIcon({ className: "", html: '<span class="report-pin" aria-hidden="true"></span>', iconSize: [30, 38], iconAnchor: [15, 30] });
  const locationContinue = byId("step-location-continue");

  async function placePin(latitude, longitude) {
    if (marker) {
      marker.setLatLng([latitude, longitude]);
    } else {
      marker = L.marker([latitude, longitude], { icon: pinIcon, draggable: true, title: "Damage location" }).addTo(map);
      marker.on("dragend", () => {
        const point = marker.getLatLng();
        placePin(point.lat, point.lng);
      });
    }
    pin = { latitude, longitude, inside: false };
    locationContinue.disabled = true;
    const ticket = ++lookup;
    say(locationStatus, "Checking the location…");
    try {
      const response = await fetch(`/api/v1/location/identify?${new URLSearchParams({ latitude, longitude })}`, { headers: { Accept: "application/json" } });
      const payload = await response.json().catch(() => ({}));
      if (ticket !== lookup) return;
      if (!response.ok) throw new Error(payload.error?.message || "The location could not be checked.");
      pin.inside = payload.inside_basey === true;
      if (pin.inside) {
        say(locationStatus, `Barangay ${payload.barangay?.name || "not identified"} — drag the pin to adjust.`, "ok");
        locationContinue.disabled = false;
      } else {
        say(locationStatus, "This point is outside Basey. Move the pin to where the damage is.", "error");
      }
    } catch (error) {
      if (ticket === lookup) say(locationStatus, error instanceof TypeError ? "No connection. Check your signal and place the pin again." : error.message, "error");
    }
  }

  map.on("click", (event) => placePin(event.latlng.lat, event.latlng.lng));

  byId("use-location").addEventListener("click", () => {
    if (!navigator.geolocation) {
      say(locationStatus, "Location isn't available on this device. Tap the map to place the pin.", "error");
      return;
    }
    say(locationStatus, "Finding your location…");
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const { latitude, longitude } = position.coords;
        map.setView([latitude, longitude], 16);
        placePin(latitude, longitude);
      },
      () => say(locationStatus, "Couldn't get your location. Tap the map to place the pin.", "error"),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 }
    );
  });

  // ---------------- Damage type / severity chips ----------------
  const damageTypeInput = byId("f-damage-type");
  const severityInput = byId("f-severity");
  const detailsContinue = byId("step-details-continue");

  const showUrgent = () => {
    byId("urgent-callout").hidden = !(severityInput.value === "life_threatening" || damageTypeInput.value === "injured_or_trapped");
  };
  function refreshDetailsValidity() {
    detailsContinue.disabled = !(damageTypeInput.value && severityInput.value);
  }
  document.querySelectorAll("#damage-type-grid .rd-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("#damage-type-grid .rd-chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
      chip.setAttribute("aria-pressed", "true");
      damageTypeInput.value = chip.dataset.value;
      showUrgent();
      refreshDetailsValidity();
    });
  });
  document.querySelectorAll("#severity-grid button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("#severity-grid button").forEach((b) => b.setAttribute("aria-pressed", "false"));
      button.setAttribute("aria-pressed", "true");
      severityInput.value = button.dataset.value;
      showUrgent();
      refreshDetailsValidity();
    });
  });

  // ---------------- People affected stepper ----------------
  const peopleInput = byId("f-people");
  const peopleCount = byId("people-count");
  function setPeople(value) {
    const clamped = Math.max(0, Math.min(100000, value));
    peopleInput.value = String(clamped);
    peopleCount.textContent = String(clamped);
  }
  byId("people-minus").addEventListener("click", () => setPeople(Number(peopleInput.value) - 1));
  byId("people-plus").addEventListener("click", () => setPeople(Number(peopleInput.value) + 1));

  // ---------------- Photos ----------------
  async function shrink(file) {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("encode failed"))), "image/jpeg", 0.8);
    });
  }

  function renderPhotoStatus() {
    const filled = photos.filter(Boolean).length;
    if (!photoStatus.dataset.kind) photoStatus.textContent = `${filled} of ${MAX_PHOTOS} photos`;
  }

  document.querySelectorAll("[data-slot-input]").forEach((input) => {
    const slot = Number(input.dataset.slotInput);
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      input.value = "";
      if (!file) return;
      const tile = input.closest(".rd-photo-tile");
      say(photoStatus, "Preparing photo…");
      try {
        const blob = await shrink(file);
        photos[slot] = blob;
        const url = URL.createObjectURL(blob);
        tile.style.backgroundImage = `url(${url})`;
        tile.classList.add("filled");
        say(photoStatus, "");
        renderPhotoStatus();
      } catch {
        say(photoStatus, "This photo type isn't supported. Use the camera or a JPEG photo.", "error");
      }
    });
  });
  document.querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const slot = Number(button.dataset.remove);
      photos[slot] = null;
      const tile = button.closest(".rd-photo-tile");
      tile.style.backgroundImage = "";
      tile.classList.remove("filled");
      say(photoStatus, "");
      renderPhotoStatus();
    });
  });
  renderPhotoStatus();

  // ---------------- Contact validity ----------------
  const nameInput = byId("f-name");
  const phoneInput = byId("f-phone");
  const consentInput = byId("f-consent");
  const sendButton = byId("send-report");
  function refreshContactValidity() {
    sendButton.disabled = !(nameInput.value.trim().length > 1 && phoneInput.value.trim().length > 6 && consentInput.checked);
  }
  [nameInput, phoneInput].forEach((input) => input.addEventListener("input", refreshContactValidity));
  consentInput.addEventListener("change", refreshContactValidity);
  refreshContactValidity();

  // ---------------- Submit ----------------
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!pin?.inside) {
      showStep("location");
      say(locationStatus, "Place the pin inside Basey where the damage is.", "error");
      byId("report-map").focus({ preventScroll: true });
      return;
    }
    const data = new FormData(form);
    data.set("latitude", String(pin.latitude));
    data.set("longitude", String(pin.longitude));
    photos.forEach((blob, index) => {
      if (blob) data.append("photo", blob, `photo-${index + 1}.jpg`);
    });
    sendButton.disabled = true;
    say(formStatus, "Sending your report…");
    try {
      const response = await fetch("/api/v1/reports", { method: "POST", body: data, headers: { Accept: "application/json" } });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 201) {
        document.querySelectorAll(".rd-step[data-step]").forEach((section) => { section.hidden = true; });
        byId("report-number").textContent = `#${payload.id}`;
        const urgent = severityInput.value === "life_threatening" || damageTypeInput.value === "injured_or_trapped";
        byId("success-urgent-note").hidden = !urgent;
        const success = byId("report-success");
        success.hidden = false;
        success.querySelector("h2").focus();
        return;
      }
      if (response.status === 429) throw new Error("Too many reports from this network right now. Wait a minute, then tap Send again. Your answers are kept.");
      throw new Error(payload.error?.message || "The report could not be sent. Try again.");
    } catch (error) {
      say(formStatus, error instanceof TypeError ? "No connection. Your answers are kept. Tap Send again when you have signal." : error.message, "error");
    } finally {
      refreshContactValidity();
    }
  });
})();
