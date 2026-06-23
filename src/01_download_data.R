# Libraries
library(dplyr)
library(here)
library(readr)

options(timeout = 6000)
download_ifnothere <- function(filename, url) {
  fs::dir_create(dirname(filename))
  if (!file.exists(filename)) {
    download.file(
      url,
      destfile = filename,
      mode = "wb",
      quiet = TRUE
    )
  }
}

# FABIO ------------------------------------------------------------------------

fabio_url <- "https://www.dropbox.com/scl/fi/78kxxsqide0c19ias660u/fabio_v2_prelim.zip?rlkey=radkdewymq0ef3v2zntbn4e5m&st=b8w8f1rc&dl=1" # nolint

fabio_file <- "fabio_v2_prelim.zip"

download_ifnothere(
  filename = here("data", "fabio", fabio_file),
  url = fabio_url
)
