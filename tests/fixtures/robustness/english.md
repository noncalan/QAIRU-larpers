# Parcel Locker Service: Software Requirements

Version 0.2. This document describes the web service that couriers and customers use to operate parcel lockers installed in apartment buildings.

## 1. Introduction

The service replaces the paper log kept by building managers. Actors: courier, customer, building manager.

## 2. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-01 | The courier shall be able to reserve a free locker compartment for a parcel before delivery. | High |
| FR-02 | The system shall send the customer a notification as soon as possible after the parcel is placed in the compartment. | High |
| FR-03 | The customer shall open the compartment with a one-time code valid for 48 hours. | High |
| FR-04 | The system shall release the compartment automatically when the code expires and notify the courier, the customer and, if needed, the building manager. | Medium |
| FR-05 | The building manager shall be able to block a compartment for maintenance. | Medium |
| FR-06 | The storage fee for an uncollected parcel is TBD. | Low |

## 3. Non-functional requirements

NFR-01. The page with the list of free compartments shall load quickly.

NFR-02. A one-time code shall remain valid for 72 hours after the parcel is placed.

NFR-03. The service shall be highly available and shall have a user-friendly interface.

NFR-04. All data exchange with the locker controller shall use the proprietary protocol defined in the controller manual.

## 4. Acceptance criteria

AC-01. A parcel placed by the courier appears in the customer's list of parcels.

AC-02. The customer receives a notification within 5 minutes after the parcel is placed.
