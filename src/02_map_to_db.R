# Libraries
library(duckdb)
library(here)
library(readODS)
library(tidyverse)

# Setup
con <- dbConnect(duckdb())
mapping_file <- here("data", "fabio_mapping.ods")

# Retrieve element from mapping table
reg_ag <-
  read_ods(mapping_file, sheet = "Regions", range = "H1:I200") |>
  drop_na()
sec_ag <-
  read_ods(mapping_file, sheet = "Sectors", range = "J1:K100") |>
  drop_na()
proc_ag <- # unused
  read_ods(mapping_file, sheet = "Processes", range = "H1:I100") |>
  drop_na()

reg_mapping <- read_ods(mapping_file, sheet = "Regions", range = "A1:E200") |>
  select(-reg_ag_code) |>
  rename(reg_ag_code = new_reg_ag_code)
sec_mapping <- read_ods(mapping_file, sheet = "Sectors", range = "A1:G200") |>
  select(-comm_ag_code) |>
  rename(comm_ag_code = new_comm_ag_code)
proc_mapping <- read_ods(
  mapping_file,
  sheet = "Processes",
  range = "A1:E200"
) |>
  select(-proc_ag_code) |>
  rename(proc_ag_code = new_proc_ag_code)

dbWriteTable(con, "map_reg", reg_mapping)
dbWriteTable(con, "map_reg_ag", reg_ag)

dbWriteTable(con, "map_comm", sec_mapping)
dbWriteTable(con, "map_comm_ag", sec_ag)

dbWriteTable(con, "map_proc", proc_mapping)
dbWriteTable(con, "map_proc_ag", proc_ag)
