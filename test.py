from cloakbrowser import launch

browser = launch()
page = browser.new_page()
page.goto("https://indiansignlanguage.org/search-dictionary")
browser.close()
