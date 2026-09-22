import sys
import re
sys.path.append(r'c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping')
from services.dax_converter import DAXConverter
from services.qlik_patterns import translate_aggr

c = DAXConverter()

test_cases = [
    """
    25 *
(
  If(
    Sum(
      Aggr(
        If(
          Floor(GRADED_DATE_DATE) >= Floor(Max({1} TOTAL GRADED_DATE_DATE)) - 179
          and Floor(GRADED_DATE_DATE) <= Floor(Max({1} TOTAL GRADED_DATE_DATE)),
          1, 0
        ),
        GRADE_ID
      )
    ) > 0,
    Sum(
      Aggr(
        If(
          Floor(GRADED_DATE_DATE) >= Floor(Max({1} TOTAL GRADED_DATE_DATE)) - 179
          and Floor(GRADED_DATE_DATE) <= Floor(Max({1} TOTAL GRADED_DATE_DATE)),
          Exp(
            -0.6931471805599453 *
            ( Floor(Max({1} TOTAL GRADED_DATE_DATE)) - Floor(GRADED_DATE_DATE) ) / 30
          )
          *
          If(
            IsNum(GRADE),
              RangeMax(0, RangeMin(4,
                If(GRADE>=90, 4,
                If(GRADE>=85, 3.7 + (GRADE-85)*0.06,
                If(GRADE>=75, 3.0 + (GRADE-75)*0.07,
                If(GRADE>=65, 2.0 + (GRADE-65)*0.10,
                If(GRADE>=50, 1.0 + (GRADE-50)*0.0667, 0))))))),
              Pick(
                Match(Upper(Trim(GRADE)),
                  'A+','A','A-','B+','B','B-','C+','C','C-','D+','D','D-','F',
                  'HD','DI','CR','P','N','PASS','FAIL'
                ),
                4.0,4.0,3.7,3.3,3.0,2.7,2.3,2.0,1.7,1.3,1.0,0.7,0.0,
                4.0,3.7,2.5,1.0,0.0,1.0,0.0
              )
          ),
          0
        ),
        GRADE_ID
      )
    )
    /
    Max(
      1e-9,
      Sum(
        Aggr(
          If(
            Floor(GRADED_DATE_DATE) >= Floor(Max({1} TOTAL GRADED_DATE_DATE)) - 179
            and Floor(GRADED_DATE_DATE) <= Floor(Max({1} TOTAL GRADED_DATE_DATE)),
            Exp(
              -0.6931471805599453 *
              ( Floor(Max({1} TOTAL GRADED_DATE_DATE)) - Floor(GRADED_DATE_DATE) ) / 30
            ),
            0
          ),
          GRADE_ID
        )
      )
    ),
    (
      Sum( Aggr(
        If(
          IsNum(GRADE),
            RangeMax(0, RangeMin(4,
              If(GRADE>=90, 4,
              If(GRADE>=85, 3.7 + (GRADE-85)*0.06,
              If(GRADE>=75, 3.0 + (GRADE-75)*0.07,
              If(GRADE>=65, 2.0 + (GRADE-65)*0.10,
              If(GRADE>=50, 1.0 + (GRADE-50)*0.0667, 0))))))),
            Pick(
              Match(Upper(Trim(GRADE)),
                'A+','A','A-','B+','B','B-','C+','C','C-','D+','D','D-','F',
                'HD','DI','CR','P','N','PASS','FAIL'
              ),
              4.0,4.0,3.7,3.3,3.0,2.7,2.3,2.0,1.7,1.3,1.0,0.7,0.0,
              4.0,3.7,2.5,1.0,0.0,1.0,0.0
            )
        ),
        GRADE_ID
      )) 
      /
      Max(1, Sum( Aggr(1, GRADE_ID) ))
    )
  )
)
    """
]

tables = [
    {
        "name": "GRADES",
        "columns": [
            {"name": "GRADE_ID"},
            {"name": "GRADED_DATE_DATE"},
            {"name": "GRADE"}
        ]
    }
]

for t in test_cases:
    dax = c.qlik_to_dax(t, tables)
    print(f'Original: {t}')
    print(f'DAX: {dax}')
    print(f'Unresolved: {c.unresolved_columns}')
    print('-'*40)
