import { expect, test } from "@playwright/test";

// The five v1 screens against a seeded deployment (tenant from EE_TENANT, dev auth).
const tenant = process.env.EE_TENANT || "bank";
const shots = process.env.EE_SHOTS;

test.beforeEach(async ({ page }) => {
  await page.addInitScript((t) => localStorage.setItem("ee.tenant", t), tenant);
});

async function shot(page: import("@playwright/test").Page, name: string) {
  if (shots) await page.screenshot({ path: `${shots}/${name}.png`, fullPage: true });
}

test("conversations: list, facets, search, viewer", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Conversations" })).toBeVisible();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await expect(page.getByText(/\d+ conversations/)).toBeVisible();
  await shot(page, "1-conversations");
  await page.locator(".facet").first().click();
  await expect(page).toHaveURL(/flag=/);
  await page.getByLabel("search").fill("transferencia");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await page.locator("tbody tr").first().click();
  await expect(page.locator(".turn").first()).toBeVisible();
  await expect(page.getByText("route", { exact: false }).first()).toBeVisible();
  await expect(page.locator(".span-row").first()).toBeVisible();
  await shot(page, "2-conversation");
});

test("scores and calibration report", async ({ page }) => {
  await page.goto("/scores");
  await expect(page.getByRole("heading", { name: "Scores & calibration" })).toBeVisible();
  await expect(page.locator(".stack").first()).toBeVisible();
  await shot(page, "3-scores");
  await page.locator("a[href^='/calibrations/']").first().click();
  await expect(page.getByText("meets target").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Hosted sets considered" })).toBeVisible();
  await shot(page, "4-calibration");
});

test("experiments: compare two runs", async ({ page }) => {
  await page.goto("/experiments");
  const rows = page.locator("tbody tr");
  await expect(rows.nth(1)).toBeVisible();
  await rows.nth(0).click();
  await rows.nth(1).click();
  await expect(page.getByRole("heading", { name: "Comparison" })).toBeVisible();
  await shot(page, "5-experiments");
});

test("annotation with the keyboard", async ({ page }) => {
  await page.goto("/annotate");
  const queue = page.getByLabel("queue");
  await expect(queue.locator("option")).not.toHaveCount(1);
  await queue.selectOption({ index: 1 });
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.locator(".turn").first()).toBeVisible();
  const first = await page.locator(".small.muted").first().textContent();
  await page.keyboard.press("1");
  await page.keyboard.press("0");
  await shot(page, "6-annotation");
  await page.keyboard.press("Enter");
  await expect(page.getByText("1 labelled this session")).toBeVisible();
  await expect(page.locator(".small.muted").first()).not.toHaveText(first || "");
});
