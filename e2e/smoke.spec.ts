import { test, expect, type Page } from "@playwright/test";

/** Skip onboarding/BootIntro so the deck renders even when the backend is down. */
async function seedAppState(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("jarvis_user_name", "E2E");
    localStorage.setItem("jarvis_ui_preset", "prime");
    sessionStorage.setItem("jarvis_intro_seen", "1");
  });
}

/**
 * Resilient composer locator across decks:
 * - Prime: "Talk to JARVIS"
 * - Overhaul: "Command JARVIS"
 * - Focus/Chat: "Message JARVIS"
 * - Terminal: "Terminal command"
 */
function composerInput(page: Page) {
  return page.getByRole("textbox", {
    name: /command|message|talk|terminal/i,
  });
}

test.describe("command deck smoke", () => {
  test.beforeEach(async ({ page }) => {
    await seedAppState(page);
  });

  test("deck UI loads", async ({ page }) => {
    await page.goto("/");

    const composer = composerInput(page);
    await expect(composer).toBeVisible({ timeout: 15_000 });

    const sendBtn = page.getByRole("button", { name: /send/i }).first();
    await expect(sendBtn).toBeVisible();
  });

  test("composer accepts input", async ({ page }) => {
    await page.goto("/");

    const composer = composerInput(page);
    await expect(composer).toBeVisible({ timeout: 15_000 });

    const command = "hello jarvis";
    await composer.fill(command);
    await expect(composer).toHaveValue(command);

    const sendBtn = page.getByRole("button", { name: /send/i }).first();
    if (await sendBtn.isVisible()) {
      await sendBtn.click();
      await expect(composer).toHaveValue("");
    } else {
      await composer.press("Enter");
      await expect(composer).toHaveValue("");
    }
  });
});
