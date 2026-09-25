(() => {
  "use strict";

  const BASEY_CENTER = [11.282, 125.069];
  const BASEY_FALLBACK_BOUNDS = [[11.2540, 124.9764], [11.5641, 125.3092]];
  const MAX_PHOTOS = 3;
  const MAX_EDGE = 1600;
  const byId = (id) => document.getElementById(id);
  const form = byId("report-form");
  const locationStatus = byId("location-status");
  const photoStatus = byId("photo-status");
  const formStatus = byId("form-status");
  const photos = [];
  let pin = null;
  let marker = null;
  let lookup = 0;

  function say(element, message, kind = "") {
    element.textContent = message;
    element.dataset.kind = kind;
  }

  const menuButton = document.querySelector(".menu-toggle");
  menuButton?.addEventListener("click", () => {
    const open = menuButton.getAttribute("aria-expanded") !== "true";
    menuButton.setAttribute("aria-expanded", String(open));
    document.querySelector(".site-nav")?.classList.toggle("is-open", open);
  });

  fetch("/api/v1/reports", { headers: { Accept: "application/json" } })
    .then((response) => {
      if (response.status === 503) {
        form.hidden = true;
        byId("report-unavailable").hidden = false;
      }
    })
    .catch(() => {});

  const map = L.map("report-map", {
    maxBounds: L.latLngBounds(BASEY_FALLBACK_BOUNDS).pad(0.3),
    maxBoundsViscosity: 0.8
  }).setView(BASEY_CENTER, 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  const pinIcon = L.divIcon({ className: "", html: '<span class="report-pin" aria-hidden="true"></span>', iconSize: [30, 38], iconAnchor: [15, 30] });

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
    const ticket = ++lookup;
    say(locationStatus, "Checking the location…");
    try {
      const response = await fetch(`/api/v1/location/identify?${new URLSearchParams({ latitude, longitude })}`, { headers: { Accept: "application/json" } });
      const payload = await response.json().catch(() => ({}));
      if (ticket !== lookup) return;
      if (!response.ok) throw new Error(payload.error?.message || "The location could not be checked.");
      pin.inside = payload.inside_basey === true;
      if (pin.inside) say(locationStatus, `Barangay ${payload.barangay?.name || "not identified"} — drag the pin to adjust.`, "ok");
      else say(locationStatus, "This point is outside Basey. Move the pin to where the damage is.", "error");
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

  function renderPhotos() {
    const list = byId("photo-previews");
    list.querySelectorAll("img").forEach((image) => URL.revokeObjectURL(image.src));
    list.replaceChildren(...photos.map((blob, index) => {
      const item = document.createElement("li");
      const image = document.createElement("img");
      image.src = URL.createObjectURL(blob);
      image.alt = `Photo ${index + 1}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove photo ${index + 1}`);
      remove.addEventListener("click", () => {
        photos.splice(index, 1);
        say(photoStatus, "");
        renderPhotos();
      });
      item.append(image, remove);
      return item;
    }));
    if (!photoStatus.dataset.kind) photoStatus.textContent = `${photos.length} of ${MAX_PHOTOS} photos`;
  }

  byId("photos").addEventListener("change", async (event) => {
    const files = [...event.target.files];
    event.target.value = "";
    say(photoStatus, "Preparing photos…");
    for (const file of files) {
      if (photos.length >= MAX_PHOTOS) {
        say(photoStatus, `You can attach up to ${MAX_PHOTOS} photos.`, "error");
        break;
      }
      try {
        photos.push(await shrink(file));
      } catch {
        say(photoStatus, "This photo type isn't supported. Use the camera or a JPEG photo.", "error");
      }
    }
    renderPhotos();
  });

  const showUrgent = () => {
    byId("urgent-callout").hidden = !(form.elements.severity.value === "life_threatening" || form.elements.damage_type.value === "injured_or_trapped");
  };
  form.elements.severity.addEventListener("change", showUrgent);
  form.elements.damage_type.addEventListener("change", showUrgent);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!pin?.inside) {
      say(locationStatus, "Place the pin inside Basey where the damage is.", "error");
      byId("report-map").scrollIntoView({ block: "center" });
      byId("report-map").focus({ preventScroll: true });
      return;
    }
    const data = new FormData(form);
    data.set("latitude", String(pin.latitude));
    data.set("longitude", String(pin.longitude));
    photos.forEach((blob, index) => data.append("photo", blob, `photo-${index + 1}.jpg`));
    const button = byId("send-report");
    button.disabled = true;
    say(formStatus, "Sending your report…");
    try {
      const response = await fetch("/api/v1/reports", { method: "POST", body: data, headers: { Accept: "application/json" } });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 201) {
        form.hidden = true;
        byId("report-number").textContent = `#${payload.id}`;
        byId("report-success").hidden = false;
        byId("report-success").querySelector("h2").focus();
        return;
      }
      if (response.status === 429) throw new Error("Too many reports from this network right now. Wait a minute, then tap Send again. Your answers are kept.");
      throw new Error(payload.error?.message || "The report could not be sent. Try again.");
    } catch (error) {
      say(formStatus, error instanceof TypeError ? "No connection. Your answers are kept. Tap Send again when you have signal." : error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
})();
