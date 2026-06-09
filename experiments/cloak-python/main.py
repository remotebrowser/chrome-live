from cloakbrowser import launch

print("launching browser...")
browser = launch(headless=False)
print("browser launched — navigate manually, press Enter to close")

page = browser.new_page()
page.goto("https://example.com")
print(f"opened: {page.title()}")

input("Press Enter to close...")
browser.close()
print("done")
