function fitAppSelectWidth(select, labels) {
  const probe = document.createElement("span");
  probe.className = "app-select-width-probe";
  document.body.append(probe);
  let longest = 0;
  labels.forEach(label => {
    probe.textContent = label;
    longest = Math.max(longest, probe.getBoundingClientRect().width);
  });
  probe.remove();
  const width = Math.max(110, Math.ceil(longest + 58));
  select.style.width = `min(${width}px, calc(100vw - 32px))`;
}

function initializeAppSelect(select, value, summary) {
  const render = () => {
    const options = [...select.querySelectorAll("[data-value]")];
    fitAppSelectWidth(select, options.map(option => option.textContent.trim()));
    options.forEach(option => {
      option.classList.toggle("selected", option.dataset.value === value.value);
      if (option.dataset.appSelectBound) return;
      option.dataset.appSelectBound = "true";
      option.addEventListener("click", () => {
        value.value = option.dataset.value;
        summary.textContent = option.textContent;
        options.forEach(item => item.classList.toggle("selected", item === option));
        select.removeAttribute("open");
      });
    });
  };
  render();
  select.addEventListener("toggle", () => { if (select.open) render(); });
  document.addEventListener("click", event => {
    if (!select.contains(event.target)) select.removeAttribute("open");
  });
}

document.querySelectorAll("[data-app-select]").forEach(select => {
  const label = select.closest(".app-select-label");
  const value = label?.querySelector("[data-app-select-value]");
  const summary = select.querySelector("[data-app-select-summary]");
  if (value && summary) initializeAppSelect(select, value, summary);
});

document.querySelectorAll("select:not([data-native-select])").forEach(nativeSelect => {
  nativeSelect.classList.add("app-select-native-source");
  const wrapper = document.createElement("div");
  wrapper.className = "app-select-enhanced";
  const customSelect = document.createElement("details");
  customSelect.className = "app-select";
  const summary = document.createElement("summary");
  summary.setAttribute("data-app-select-summary", "");
  const menu = document.createElement("div");
  menu.className = "app-select-menu";
  customSelect.append(summary, menu);
  wrapper.append(customSelect);
  nativeSelect.insertAdjacentElement("afterend", wrapper);

  const sync = () => {
    wrapper.classList.toggle("disabled", nativeSelect.disabled);
    const selectedOption = nativeSelect.options[nativeSelect.selectedIndex];
    summary.textContent = selectedOption?.textContent || "선택";
    menu.replaceChildren();
    [...nativeSelect.options].forEach(option => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.value = option.value;
      button.textContent = option.textContent;
      button.disabled = option.disabled;
      button.classList.toggle("selected", option.selected);
      button.addEventListener("click", () => {
        nativeSelect.value = option.value;
        nativeSelect.dispatchEvent(new Event("change", { bubbles: true }));
        sync();
        customSelect.removeAttribute("open");
      });
      menu.append(button);
    });
    fitAppSelectWidth(
      customSelect,
      [...nativeSelect.options].map(option => option.textContent.trim()),
    );
  };
  sync();
  nativeSelect.addEventListener("change", sync);
  customSelect.addEventListener("toggle", () => {
    sync();
    if (nativeSelect.disabled) customSelect.removeAttribute("open");
  });
  new MutationObserver(sync).observe(nativeSelect, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["disabled", "selected", "label"],
  });
  document.addEventListener("click", event => {
    if (!customSelect.contains(event.target)) customSelect.removeAttribute("open");
  });
});

async function copyWalletAddress(button) {
  const address = button.dataset.copyAddress || "";
  if (!address) return;
  const originalLabel = button.getAttribute("aria-label") || "지갑주소 복사";
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(address);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = address;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.append(textarea);
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) throw new Error("copy failed");
    }
    button.classList.add("copied");
    button.setAttribute("aria-label", "지갑주소 복사 완료");
    button.title = "복사 완료";
    window.setTimeout(() => {
      button.classList.remove("copied");
      button.setAttribute("aria-label", originalLabel);
      button.title = "지갑주소 복사";
    }, 1000);
  } catch (_error) {
    button.title = "복사하지 못했습니다";
  }
}

document.querySelectorAll("[data-copy-address]").forEach(button => {
  button.addEventListener("click", () => copyWalletAddress(button));
});
